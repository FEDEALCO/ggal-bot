"""
test_execution_pipeline.py
============================
Tests de sanity para lo agregado en la Fase 1/2/3 (config, order_gateway,
market_data_feed, mid_price_exec, strategy.delta_hedger), SIN pyRofex ni
conexion real: valida que el modo simulado (pyRofex no instalado) responda
de forma consistente, que el parser de simbolos de opciones funcione contra
casos sinteticos, y que el motor de ejecucion a mid-price arme las ordenes
esperadas. Correr con:

    python -m ggal_bot.validation.test_execution_pipeline
"""

from __future__ import annotations

import os
import sys

# Ver test_quant_engine.py: permite correr este archivo tanto como modulo
# (`python -m ggal_bot.validation.test_execution_pipeline`, recomendado)
# como script directo.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timedelta, timezone

# Debe importarse ANTES que ggal_bot.execution.order_gateway/run_bot para
# redirigir el CSV de auditoria de shadow trading a un path temporal (ver
# ese modulo: evita contaminar logs/shadow_trades.csv real, bug corregido
# en la auditoria del 2026-08-27, seccion 3.3).
from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS, BrokerConfig
from ggal_bot.data.option_chain import OrderBookSnapshot
from ggal_bot.data.market_data_feed import MarketDataFeed, _third_friday_on_or_after, _business_days_between
from ggal_bot.data.option_chain import OptionChain
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.execution.order_gateway import (
    OrderGateway, OrderSide, OrderStatus, send_order, cancel_order, get_account_positions,
)
from ggal_bot.execution.mid_price_exec import MidPriceExecutionEngine
from ggal_bot.portfolio.portfolio import Position
from ggal_bot.strategy.delta_hedger import DeltaHedgingEngine
from ggal_bot.strategy.vol_arbitrage import TradeSignal
from run_bot import GgalOptionsBot


def test_broker_config_validate_reports_missing_credentials():
    broker = BrokerConfig(user="", password="", account="", environment="REMARKET")
    ok, msg = broker.validate()
    assert ok is False
    assert "PYROFEX_USER" in msg


def test_broker_config_validate_ok_with_credentials():
    broker = BrokerConfig(user="u", password="p", account="a", environment="REMARKET")
    ok, msg = broker.validate()
    assert ok is True
    assert msg == ""


def test_send_order_and_cancel_simulated_without_pyrofex():
    """Sin pyRofex instalado, send_order/cancel_order deben degradar a modo simulado, nunca lanzar."""
    response = send_order(ticker="GFGC5200O", side=OrderSide.BUY, size=1, price=100.0)
    assert response["status"] in ("simulated", "sent", "error")  # nunca una excepcion no controlada
    cancel_response = cancel_order(client_order_id=response.get("clOrdId", "abc123"))
    assert cancel_response["status"] in ("simulated", "cancelled", "error")


def test_get_account_positions_simulated_without_pyrofex():
    result = get_account_positions()
    assert "positions" in result


def test_order_gateway_send_marks_state():
    gateway = OrderGateway()
    from ggal_bot.execution.order_gateway import OrderRequest, OrderTypeEnum
    request = OrderRequest(symbol="GFGC5200O", side=OrderSide.BUY, quantity=1, price=100.0,
                            order_type=OrderTypeEnum.LIMIT)
    state = gateway.send(request, reference_price=99.5)
    assert state.status in (OrderStatus.NEW, OrderStatus.REJECTED)
    assert gateway.get_state(request.client_order_id) is state


def test_mid_price_exec_submits_at_mid_for_illiquid_book():
    gateway = OrderGateway()
    engine = MidPriceExecutionEngine(gateway)
    # spread relativo grande (>2%) -> se considera ilíquido -> cotiza a mid, no cruza
    illiquid_book = OrderBookSnapshot("GFGC5200O", bid=95.0, ask=105.0, bid_size=50, ask_size=50)
    state = engine.submit(
        symbol="GFGC5200O", book=illiquid_book, side=OrderSide.BUY, quantity=1,
        spot_reference=5200.0, aggressive=False,
    )
    assert abs(state.request.price - illiquid_book.mid) < 1e-6
    assert engine.open_order_count() == 1


def test_mid_price_exec_aggressive_crosses_spread():
    gateway = OrderGateway()
    engine = MidPriceExecutionEngine(gateway)
    book = OrderBookSnapshot("GGAL", bid=5199.0, ask=5201.0, bid_size=500, ask_size=500)
    state = engine.submit(
        symbol="GGAL", book=book, side=OrderSide.BUY, quantity=350,
        spot_reference=5200.0, aggressive=True,
    )
    assert state.request.price == book.ask  # cruza el spread: compra al ask


def test_mid_price_exec_cancels_on_underlying_move():
    gateway = OrderGateway()
    engine = MidPriceExecutionEngine(gateway)
    book = OrderBookSnapshot("GFGC5200O", bid=99.0, ask=101.0, bid_size=50, ask_size=50)
    state = engine.submit(
        symbol="GFGC5200O", book=book, side=OrderSide.BUY, quantity=1,
        spot_reference=5200.0, aggressive=False,
    )
    # Simular que el subyacente salto mas alla del umbral configurado
    moved_spot = 5200.0 * (1 + SETTINGS.execution.underlying_move_cancel_pct * 2)
    engine.monitor_and_reprice(current_books={"GFGC5200O": book}, current_spot=moved_spot)
    final_state = gateway.get_state(state.request.client_order_id)
    assert final_state.status == OrderStatus.CANCELLED
    assert engine.open_order_count() == 0


def test_delta_hedging_engine_execute_hedge_submits_aggressive_order():
    gateway = OrderGateway()
    engine = MidPriceExecutionEngine(gateway)
    hedger = DeltaHedgingEngine(delta_band=150.0)
    contado_book = OrderBookSnapshot("GGAL", bid=5199.0, ask=5201.0, bid_size=500, ask_size=500)

    state = hedger.execute_hedge(
        portfolio_delta=-500.0, contado_book=contado_book, futuro_book=None,
        mid_price_engine=engine,
    )
    assert state is not None
    assert state.request.side is OrderSide.BUY
    assert state.request.quantity == 350.0  # excedente sobre la banda (500-150)
    assert state.request.price == contado_book.ask  # hedge agresivo: cruza el spread


def test_delta_hedging_engine_returns_none_when_within_band():
    gateway = OrderGateway()
    engine = MidPriceExecutionEngine(gateway)
    hedger = DeltaHedgingEngine(delta_band=150.0)
    contado_book = OrderBookSnapshot("GGAL", bid=5199.0, ask=5201.0, bid_size=500, ask_size=500)
    state = hedger.execute_hedge(
        portfolio_delta=50.0, contado_book=contado_book, futuro_book=None, mid_price_engine=engine,
    )
    assert state is None


def test_bootstrap_universe_classifies_by_underlying_field_without_symbol_prefix():
    """
    Regresion del caso real reportado: un ALYC devolvio 878 instrumentos sin
    NINGUNO cuyo simbolo contuviera 'GGAL'/'GFG' como texto, porque las
    opciones no seguian ese patron de nombre. bootstrap_universe debe poder
    identificarlas igual usando los campos semanticos que SI trae pyRofex
    (underlying, cficode, strike), sin depender del texto del simbolo.
    """
    fake_instruments = [
        {"instrumentId": {"symbol": "MERV - XMEV - GGAL - 24hs"}, "underlying": "GGAL",
         "cficode": "ESXXXX", "strike": 0, "maturityDate": None},
        # Simbolo sin "GGAL"/"GFG" como texto: solo se identifica por underlying/cficode/strike.
        {"instrumentId": {"symbol": "RAROSYM123"}, "underlying": "GGAL",
         "cficode": "OCASPS", "strike": 5400, "maturityDate": "20261016"},
        {"instrumentId": {"symbol": "OTHERSYM456"}, "underlying": "GGAL",
         "cficode": "OPASPS", "strike": 4800, "maturityDate": "20261016"},
        # Futuro de GGAL: mismo underlying, pero sin strike y cficode no-opcion -> debe descartarse.
        {"instrumentId": {"symbol": "GGAL/OCT26"}, "underlying": "GGAL",
         "cficode": "FXXXXX", "strike": 0, "maturityDate": "20261016"},
        # Otra especie: debe descartarse por underlying distinto.
        {"instrumentId": {"symbol": "MERV - XMEV - PAMP - 24hs"}, "underlying": "PAMP",
         "cficode": "ESXXXX", "strike": 0, "maturityDate": None},
    ]

    feed = MarketDataFeed(on_book_update=lambda *_: None)
    feed._fetch_instruments = lambda: fake_instruments
    chain = OptionChain()
    tickers = feed.bootstrap_universe(chain)

    quotes_by_symbol = {q.symbol: q for q in chain.all_quotes()}
    assert set(quotes_by_symbol.keys()) == {"RAROSYM123", "OTHERSYM456"}
    assert quotes_by_symbol["RAROSYM123"].option_type is OptionType.CALL
    assert quotes_by_symbol["RAROSYM123"].strike == 5400.0
    assert quotes_by_symbol["OTHERSYM456"].option_type is OptionType.PUT
    assert "GGAL/OCT26" not in quotes_by_symbol  # futuro: no es una opcion
    assert "MERV - XMEV - GGAL - 24hs" in tickers  # el contado siempre se suscribe


def test_option_symbol_parser_synthetic_case():
    """
    Valida la heuristica de fallback de bootstrap_universe contra un simbolo
    sintetico (GFGC + 4 digitos de strike + letra de mes). No sustituye una
    prueba contra el listado real de instrumentos del ALYC.
    """
    feed = MarketDataFeed(on_book_update=lambda *_: None)
    parsed = feed._parse_option_symbol("GFGC5200F", "GFGC")  # F = 6to mes (Junio)
    assert parsed is not None
    strike, expiry = parsed
    assert strike == 5200.0
    assert expiry.month == 6
    assert expiry.weekday() == 4  # el fallback asume tercer viernes del mes


def test_third_friday_helper_returns_a_friday():
    d = _third_friday_on_or_after(date(2026, 1, 1), 3)
    assert d.month == 3
    assert d.weekday() == 4


def test_business_days_between_excludes_weekends():
    # 2026-01-05 (lunes) a 2026-01-09 (viernes) = 4 dias habiles
    days = _business_days_between(date(2026, 1, 5), date(2026, 1, 9))
    assert days == 4


def test_act_on_signal_does_not_reenter_same_symbol_across_cycles_in_shadow_mode():
    """
    Regresion de un bug real reportado corriendo el bot en modo shadow: la
    señal de smile dislocation persiste mientras la sonrisa no se corrige,
    asi que sin memoria de la posicion ya abierta, run_bot._act_on_signal()
    reentraba la MISMA base en cada ciclo (una orden nueva cada ~4s, sin
    limite - ver logs/shadow_trades.csv real pegado por el usuario). Con
    SETTINGS.shadow.enabled=True el fill es sincronico, asi que la segunda
    llamada a _act_on_signal() con la misma señal debe quedar bloqueada por
    la guarda de posicion existente en self.portfolio.
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        book = OrderBookSnapshot("GFGC5200O", bid=99.0, ask=101.0, bid_size=50, ask_size=50)
        from ggal_bot.data.option_chain import OptionQuote
        option_quote = OptionQuote(
            symbol="GFGC5200O", strike=5200.0, expiry=date(2026, 9, 18), option_type=OptionType.CALL,
            book=book, days_calendar=30, days_business=21,
        )
        option_quote.greeks = {"delta": 0.5, "gamma": 0.001, "vega": 2.0, "theta": -1.0, "rho": 0.1, "price": 100.0}
        bot.option_chain.upsert_quote(option_quote)

        signal = TradeSignal(symbol="GFGC5200O", action="buy", reason="test", iv_dislocation_vol_points=5.0)

        bot._act_on_signal(signal, spot=5200.0)
        assert bot._position_quantity("GFGC5200O") == 1

        # Misma señal reevaluada en un ciclo posterior (asi es exactamente
        # como se re-emite en recompute_cycle mientras la sonrisa no se
        # corrija): NO debe pyramidear.
        bot._act_on_signal(signal, spot=5200.0)
        assert bot._position_quantity("GFGC5200O") == 1
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_maybe_hedge_records_fill_so_delta_reflects_the_hedge():
    """
    Regresion de un bug real reportado por el usuario (via el dashboard: una
    posicion de delta-hedge de ~38.000 acciones de GGAL y un PnL no
    realizado de ~$17 millones, en un bot con un techo de capital de
    $1.000.000): antes de esta correccion, _maybe_hedge() nunca registraba
    el fill de la orden de cobertura en self.portfolio, asi que
    total_greeks()["delta"] jamas reflejaba la cobertura recien ejecutada -
    cada ciclo siguiente volvia a ver el MISMO delta "fuera de banda" y
    disparaba OTRA orden de cobertura identica, sin limite (rehedge sin fin).

    Este test reproduce el patron exacto: una posicion de OPCIONES con delta
    500 (banda=150) dispara una cobertura de -350 acciones; tras
    registrarla, el delta total debe caer EXACTAMENTE al borde de la banda
    (150) y un segundo llamado a _maybe_hedge() en el mismo estado NO debe
    generar una segunda orden (needs_hedge ya da False).
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        # Posicion de opciones con delta total = 10 * 100 * 0.5 = 500 (excede la banda de 150).
        bot.portfolio.add(Position(
            symbol="GFGC5200O", quantity=10, multiplier=100.0,
            greeks_per_unit={"delta": 0.5, "gamma": 0.01, "vega": 5.0, "theta": -1.0},
        ))
        bot._spot_book = OrderBookSnapshot(
            SETTINGS.instruments.contado_ticker, bid=5199.0, ask=5201.0, bid_size=500, ask_size=500,
        )

        totals = bot.portfolio.total_greeks()
        assert totals["delta"] == 500.0
        bot._maybe_hedge(totals, spot=5200.0)

        hedge_positions = [p for p in bot.portfolio.positions if p.symbol == SETTINGS.instruments.contado_ticker]
        assert len(hedge_positions) == 1
        assert hedge_positions[0].quantity == -350.0  # excedente sobre la banda (500-150), vendido
        assert hedge_positions[0].multiplier == 1.0    # accion, NO el multiplicador de opciones (100)
        assert hedge_positions[0].greeks_per_unit is None  # marca de "subyacente", no opcion

        # El delta total ahora debe reflejar la cobertura: 500 + (-350)*1*1 = 150 (borde exacto de la banda).
        totals_after = bot.portfolio.total_greeks()
        assert totals_after["delta"] == 150.0
        assert not bot.delta_hedger.needs_hedge(totals_after["delta"])

        # Sin esta correccion, este segundo llamado habria vuelto a agregar
        # OTRA posicion de -350 (el mismo delta de 500 "visto" de nuevo).
        bot._maybe_hedge(totals_after, spot=5200.0)
        hedge_positions_after = [p for p in bot.portfolio.positions if p.symbol == SETTINGS.instruments.contado_ticker]
        assert len(hedge_positions_after) == 1  # no crecio: needs_hedge() ya daba False
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_capital_available_ars_excludes_delta_hedge_underlying_position():
    """
    La posicion del subyacente que deja el delta-hedger (greeks_per_unit=None)
    NO debe contar como "capital comprometido" para el sizing de nuevas
    entradas de opciones bajo weekly_asymmetric - son presupuestos
    separados (ver _capital_available_ars()).
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        before = bot._capital_available_ars()
        bot.portfolio.add(Position(
            symbol=SETTINGS.instruments.contado_ticker, quantity=-350.0, multiplier=1.0,
            greeks_per_unit=None, entry_price=5200.0,
        ))
        after = bot._capital_available_ars()
        assert after == before  # la posicion del subyacente no debe reducir el capital disponible
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_on_book_update_stamps_spot_last_update_at():
    """
    _on_book_update() debe marcar _spot_last_update_at SOLO cuando llega una
    punta del SPOT (contado/futuro), no cuando llega una punta de una
    opcion individual (ver docstring de _is_market_data_stale: el spot es el
    dato mas critico y el unico que falla de forma atomica cuando cae el feed).

    BUG REAL CORREGIDO (ver docs/AUDITORIA_MAESTRA_2026-08-27.md, seguimiento
    del 2026-08-31): _spot_last_update_at ahora se deriva de `book.as_of` (la
    hora real de origen del dato, ver OrderBookSnapshot.as_of), no de
    `datetime.now()` en el momento del despacho - por eso `before` se captura
    ANTES de construir `spot_book` (cuyo `as_of` por default queda fijado en
    el momento de esa construccion, no en el de _on_book_update()).
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        assert bot._spot_last_update_at is None

        option_book = OrderBookSnapshot("GFGC5200O", bid=99.0, ask=101.0, bid_size=50, ask_size=50)
        bot._on_book_update("GFGC5200O", option_book)
        assert bot._spot_last_update_at is None  # una opcion no cuenta

        before = datetime.now(timezone.utc)
        spot_book = OrderBookSnapshot(
            SETTINGS.instruments.contado_ticker, bid=5199.0, ask=5201.0, bid_size=500, ask_size=500,
        )
        bot._on_book_update(SETTINGS.instruments.contado_ticker, spot_book)
        after = datetime.now(timezone.utc)
        assert bot._spot_last_update_at is not None
        assert before <= bot._spot_last_update_at <= after
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_is_market_data_stale_false_before_any_update():
    """Antes del primer dato, no se considera 'stale' (ese caso ya lo cubre por separado self._spot_book is None)."""
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        now = datetime.now(timezone.utc)
        assert bot._market_data_staleness_seconds(now) is None
        assert bot._is_market_data_stale(now) is False
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_is_market_data_stale_false_within_threshold():
    original_enabled = SETTINGS.shadow.enabled
    original_threshold = SETTINGS.risk.max_market_data_staleness_seconds
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.max_market_data_staleness_seconds = 60.0
    try:
        bot = GgalOptionsBot()
        now = datetime.now(timezone.utc)
        bot._spot_last_update_at = now - timedelta(seconds=30)  # dentro del umbral de 60s
        assert bot._market_data_staleness_seconds(now) == 30.0
        assert bot._is_market_data_stale(now) is False
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.max_market_data_staleness_seconds = original_threshold


def test_is_market_data_stale_true_beyond_threshold():
    original_enabled = SETTINGS.shadow.enabled
    original_threshold = SETTINGS.risk.max_market_data_staleness_seconds
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.max_market_data_staleness_seconds = 60.0
    try:
        bot = GgalOptionsBot()
        now = datetime.now(timezone.utc)
        bot._spot_last_update_at = now - timedelta(seconds=90)  # supera el umbral de 60s
        assert bot._market_data_staleness_seconds(now) == 90.0
        assert bot._is_market_data_stale(now) is True
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.max_market_data_staleness_seconds = original_threshold


def test_is_market_data_stale_boundary_is_not_stale():
    """Exactamente en el umbral (ni un segundo mas) todavia NO se considera stale (estricto >, no >=)."""
    original_enabled = SETTINGS.shadow.enabled
    original_threshold = SETTINGS.risk.max_market_data_staleness_seconds
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.max_market_data_staleness_seconds = 60.0
    try:
        bot = GgalOptionsBot()
        now = datetime.now(timezone.utc)
        bot._spot_last_update_at = now - timedelta(seconds=60)
        assert bot._is_market_data_stale(now) is False
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.risk.max_market_data_staleness_seconds = original_threshold


ALL_TESTS = [
    test_broker_config_validate_reports_missing_credentials,
    test_broker_config_validate_ok_with_credentials,
    test_send_order_and_cancel_simulated_without_pyrofex,
    test_get_account_positions_simulated_without_pyrofex,
    test_order_gateway_send_marks_state,
    test_mid_price_exec_submits_at_mid_for_illiquid_book,
    test_mid_price_exec_aggressive_crosses_spread,
    test_mid_price_exec_cancels_on_underlying_move,
    test_delta_hedging_engine_execute_hedge_submits_aggressive_order,
    test_delta_hedging_engine_returns_none_when_within_band,
    test_bootstrap_universe_classifies_by_underlying_field_without_symbol_prefix,
    test_option_symbol_parser_synthetic_case,
    test_third_friday_helper_returns_a_friday,
    test_business_days_between_excludes_weekends,
    test_act_on_signal_does_not_reenter_same_symbol_across_cycles_in_shadow_mode,
    test_maybe_hedge_records_fill_so_delta_reflects_the_hedge,
    test_capital_available_ars_excludes_delta_hedge_underlying_position,
    test_on_book_update_stamps_spot_last_update_at,
    test_is_market_data_stale_false_before_any_update,
    test_is_market_data_stale_false_within_threshold,
    test_is_market_data_stale_true_beyond_threshold,
    test_is_market_data_stale_boundary_is_not_stale,
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
