"""
test_strategy_selector.py
============================
Tests de sanity para el selector de estrategia activa de run_bot.py (ver
config.StrategyConfig / GGAL_BOT_ACTIVE_STRATEGY):

    - GgalOptionsBot.__init__ arma la estrategia correcta segun
      SETTINGS.strategy.active ("weekly_asymmetric" por defecto,
      "vol_arbitrage" como alternativa), con fallback seguro si el valor
      configurado es invalido.
    - GgalOptionsBot._run_weekly_asymmetric_cycle() reconcilia las salidas
      (RiskManager.evaluate_position_exit) ANTES de evaluar entradas
      nuevas, de forma que el capital liberado por un cierre este
      disponible para el sizing de una entrada en el MISMO ciclo (ver
      GgalOptionsBot._capital_available_ars()).

Correr con:
    python -m ggal_bot.validation.test_strategy_selector
"""

from __future__ import annotations

import math
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timedelta, timezone

# Debe importarse ANTES que run_bot/ggal_bot.execution.order_gateway para
# redirigir el CSV de auditoria de shadow trading a un path temporal (ver
# ese modulo: evita contaminar logs/shadow_trades.csv real, bug corregido
# en la auditoria del 2026-08-27, seccion 3.3).
from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionQuote, OrderBookSnapshot
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Position
from ggal_bot.strategy.vol_arbitrage import VolatilityArbitrageStrategy
from ggal_bot.strategy.weekly_asymmetric import WeeklyAsymmetricStrategy
from run_bot import GgalOptionsBot


def _quote(symbol, strike, iv, spot_ref, days_biz, expiry=date(2026, 9, 4),
           option_type=OptionType.CALL, greeks=None, bid=99.0, ask=101.0, as_of=None):
    book_kwargs = dict(bid=bid, ask=ask, bid_size=100, ask_size=100, last_volume=1000.0)
    if as_of is not None:
        book_kwargs["as_of"] = as_of
    book = OrderBookSnapshot(symbol, **book_kwargs)
    q = OptionQuote(symbol, strike=strike, expiry=expiry, option_type=option_type,
                     book=book, days_calendar=days_biz + 2, days_business=days_biz)
    q.iv = iv
    q.spot_ref = spot_ref
    q.greeks = greeks
    return q


# ---------------------------------------------------------------------------
# Seleccion de estrategia en GgalOptionsBot.__init__
# ---------------------------------------------------------------------------

def test_default_weekly_asymmetric_wires_strategy_and_position_sizer():
    original = SETTINGS.strategy.active
    SETTINGS.strategy.active = "weekly_asymmetric"
    try:
        bot = GgalOptionsBot()
        assert bot.active_strategy_name == "weekly_asymmetric"
        assert isinstance(bot.strategy, WeeklyAsymmetricStrategy)
        assert bot.position_sizer is not None
    finally:
        SETTINGS.strategy.active = original


def test_vol_arbitrage_selection_leaves_position_sizer_unset():
    original = SETTINGS.strategy.active
    SETTINGS.strategy.active = "vol_arbitrage"
    try:
        bot = GgalOptionsBot()
        assert bot.active_strategy_name == "vol_arbitrage"
        assert isinstance(bot.strategy, VolatilityArbitrageStrategy)
        assert bot.position_sizer is None
    finally:
        SETTINGS.strategy.active = original


def test_invalid_active_strategy_falls_back_to_weekly_asymmetric():
    original = SETTINGS.strategy.active
    SETTINGS.strategy.active = "esto_no_existe"
    try:
        bot = GgalOptionsBot()
        assert bot.active_strategy_name == "weekly_asymmetric"
        assert isinstance(bot.strategy, WeeklyAsymmetricStrategy)
    finally:
        SETTINGS.strategy.active = original


# ---------------------------------------------------------------------------
# _capital_available_ars(): capital comprometido baja a 0 cuando la
# posicion se cierra (quantity=0)
# ---------------------------------------------------------------------------

def test_capital_available_ars_reflects_open_and_closed_positions():
    original = SETTINGS.strategy.active
    SETTINGS.strategy.active = "weekly_asymmetric"
    try:
        bot = GgalOptionsBot()
        # greeks_per_unit poblado: asi es como _act_on_entry_signal() agrega
        # SIEMPRE una posicion de opciones real (ver ese metodo, requiere
        # quote.greeks is not None antes de agregar la Position) - es
        # tambien la marca que _capital_available_ars() usa para distinguir
        # una opcion (cuenta como capital comprometido) de la posicion del
        # subyacente que deja el delta-hedger (greeks_per_unit=None, no
        # cuenta - ver test_execution_pipeline.py,
        # test_capital_available_ars_excludes_delta_hedge_underlying_position).
        bot.portfolio.add(Position(
            symbol="GFGC5100O", quantity=5, multiplier=100.0, entry_price=100.0,
            greeks_per_unit={"delta": 0.5, "gamma": 0.01, "vega": 5.0, "theta": -1.0},
        ))
        # comprometido = 5 * 100.0 * 100.0 = 50,000
        assert bot._capital_available_ars() == SETTINGS.long_first.max_capital_ars - 50_000.0

        bot.portfolio.positions[0].quantity = 0.0  # simula el cierre (ver _act_on_exit_signal)
        assert bot._capital_available_ars() == SETTINGS.long_first.max_capital_ars
    finally:
        SETTINGS.strategy.active = original


# ---------------------------------------------------------------------------
# _run_weekly_asymmetric_cycle(): las salidas se reconcilian ANTES de las
# entradas, y el capital liberado alcanza para dimensionar la entrada nueva
# EN EL MISMO CICLO (si el orden fuera al reves, este test fallaria: la
# entrada se rechazaria por falta de capital).
# ---------------------------------------------------------------------------

def test_weekly_asymmetric_cycle_reconciles_exit_before_sizing_new_entry():
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_max_capital = SETTINGS.long_first.max_capital_ars
    original_risk_pct = SETTINGS.long_first.max_risk_pct_per_trade
    original_ta_enabled = SETTINGS.technical_analysis.enabled
    SETTINGS.strategy.active = "weekly_asymmetric"
    SETTINGS.shadow.enabled = True  # fills sincronicos, determinismo (ver test_execution_pipeline.py)
    # Filtro tecnico 1D desactivado deliberadamente aca: este test corre el
    # ciclo completo via _run_weekly_asymmetric_cycle(), que ahora tambien
    # refresca TechnicalAnalysisEngine - sin red disponible en este entorno,
    # cae al generador synthetic SIN semilla fija (aleatorio de verdad, ver
    # _resolve_bars_source()), lo que volveria este test intermitente
    # (BULLISH/BEARISH/NEUTRAL al azar filtrando la Call candidata). El
    # objetivo de este test es el ORDEN salida-antes-que-entrada, no el
    # filtro de tendencia (ya cubierto en test_long_first_mode.py) - se
    # aisla ese efecto apagandolo, igual que test_technical_filter_disabled_ignores_trend.
    SETTINGS.technical_analysis.enabled = False
    # Capital ajustado para que la entrada SOLO sea sizeable si el capital
    # comprometido por la posicion que se cierra este ciclo ya fue liberado:
    # comprometido=50,000; techo=55,000 -> disponible ANTES de cerrar = 5,000
    # (insuficiente para 1 contrato de $10,000) vs. disponible DESPUES de
    # cerrar = 55,000 (alcanza para 5 contratos).
    SETTINGS.long_first.max_capital_ars = 55_000.0
    SETTINGS.long_first.max_risk_pct_per_trade = 1.0
    try:
        bot = GgalOptionsBot()

        # -- Posicion existente que va a disparar Stop Loss este ciclo ------
        entry_time = datetime.now(timezone.utc) - timedelta(hours=2)
        bot.portfolio.add(Position(
            symbol="GFGC5300O", quantity=5, multiplier=100.0,
            entry_price=100.0, entry_time=entry_time, expiry=date(2026, 9, 4),
        ))
        # Precio actual muy por debajo de la prima de entrada (-60%): dispara stop_loss.
        exit_quote = _quote("GFGC5300O", 5300, iv=None, spot_ref=5200.0, days_biz=3, bid=38.0, ask=42.0)
        exit_quote.iv = None  # fuera de la superficie de vol (no debe competir por señales de entrada)
        bot.option_chain.upsert_quote(exit_quote)

        # -- Candidata de entrada: base barata dentro de horizonte/banda ----
        spot = 5200.0

        def smile_iv(strike: float) -> float:
            x = math.log(strike / spot)
            return 0.45 + 6.0 * x * x

        filler_strikes = [4700, 4900, 5000, 5100, 5300 - 100, 5400, 5500, 5700]
        for k in filler_strikes:
            q = _quote(f"GFGC{k}O", k, smile_iv(k), spot, days_biz=3)
            bot.option_chain.upsert_quote(q)
            bot._recent_volumes[q.symbol] = 1000.0

        target = _quote(
            "GFGC5150O", 5150, smile_iv(5150) - 0.08, spot, days_biz=3,
            greeks={"gamma": 0.01, "vega": 5.0, "delta": 0.5, "theta": -1.0},
        )
        bot.option_chain.upsert_quote(target)
        bot._recent_volumes[target.symbol] = 1000.0

        all_signals = bot._run_weekly_asymmetric_cycle(spot)

        # 1) La posicion existente quedo cerrada (Stop Loss reconciliado).
        assert bot._position_quantity("GFGC5300O") == 0

        # 2) La entrada nueva se abrio Y con el tamaño que solo es posible
        #    si el capital liberado por el cierre ya estaba disponible.
        assert bot._position_quantity("GFGC5150O") == 5

        # 3) Las señales devueltas incluyen tanto la salida como la entrada.
        reasons = [type(s).__name__ for s in all_signals]
        assert "ExitSignal" in reasons
        assert "EntrySignal" in reasons
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.long_first.max_capital_ars = original_max_capital
        SETTINGS.long_first.max_risk_pct_per_trade = original_risk_pct
        SETTINGS.technical_analysis.enabled = original_ta_enabled


def test_weekly_asymmetric_cycle_skips_entries_and_spreads_when_market_data_stale():
    """
    Regresion de la guardia de staleness de datos de mercado (ver
    RiskConfig.max_market_data_staleness_seconds / run_bot._is_market_data_stale,
    agregada tras un caso real de caida sostenida de conectividad con
    data912.com reportado por el usuario). Mismo fixture que
    test_weekly_asymmetric_cycle_reconciles_exit_before_sizing_new_entry
    (Stop Loss a reconciliar + una entrada candidata perfectamente valida),
    pero con self._spot_last_update_at simulando datos de 120s de antiguedad
    (supera el umbral default de 60s): la SALIDA debe reconciliarse igual,
    pero la ENTRADA candidata NO debe abrirse ni generar señal - la guardia
    bloquea los pasos 2/3 del ciclo por completo mientras dure la caida.
    """
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_max_capital = SETTINGS.long_first.max_capital_ars
    original_risk_pct = SETTINGS.long_first.max_risk_pct_per_trade
    original_ta_enabled = SETTINGS.technical_analysis.enabled
    original_staleness_threshold = SETTINGS.risk.max_market_data_staleness_seconds
    SETTINGS.strategy.active = "weekly_asymmetric"
    SETTINGS.shadow.enabled = True
    SETTINGS.technical_analysis.enabled = False
    SETTINGS.long_first.max_capital_ars = 55_000.0
    SETTINGS.long_first.max_risk_pct_per_trade = 1.0
    SETTINGS.risk.max_market_data_staleness_seconds = 60.0
    try:
        bot = GgalOptionsBot()

        # -- Posicion existente que va a disparar Stop Loss este ciclo ------
        entry_time = datetime.now(timezone.utc) - timedelta(hours=2)
        bot.portfolio.add(Position(
            symbol="GFGC5300O", quantity=5, multiplier=100.0,
            entry_price=100.0, entry_time=entry_time, expiry=date(2026, 9, 4),
        ))
        exit_quote = _quote("GFGC5300O", 5300, iv=None, spot_ref=5200.0, days_biz=3, bid=38.0, ask=42.0)
        exit_quote.iv = None
        bot.option_chain.upsert_quote(exit_quote)

        # -- Candidata de entrada valida (identica al test de reconciliacion) --
        spot = 5200.0

        def smile_iv(strike: float) -> float:
            x = math.log(strike / spot)
            return 0.45 + 6.0 * x * x

        filler_strikes = [4700, 4900, 5000, 5100, 5300 - 100, 5400, 5500, 5700]
        for k in filler_strikes:
            q = _quote(f"GFGC{k}O", k, smile_iv(k), spot, days_biz=3)
            bot.option_chain.upsert_quote(q)
            bot._recent_volumes[q.symbol] = 1000.0

        target = _quote(
            "GFGC5150O", 5150, smile_iv(5150) - 0.08, spot, days_biz=3,
            greeks={"gamma": 0.01, "vega": 5.0, "delta": 0.5, "theta": -1.0},
        )
        bot.option_chain.upsert_quote(target)
        bot._recent_volumes[target.symbol] = 1000.0

        # -- Datos "viejos": la ultima actualizacion del spot fue hace 120s,
        # por encima del umbral configurado de 60s (simula la caida real de
        # conectividad con data912.com reportada por el usuario). --
        bot._spot_last_update_at = datetime.now(timezone.utc) - timedelta(seconds=120)

        all_signals = bot._run_weekly_asymmetric_cycle(spot)

        # 1) La salida SI se reconcilia: no depende de la frescura del spot.
        assert bot._position_quantity("GFGC5300O") == 0

        # 2) La entrada candidata NO se abre: la guardia de staleness bloqueo
        #    por completo los pasos de entradas/spreads este ciclo.
        assert bot._position_quantity("GFGC5150O") == 0

        reasons = [type(s).__name__ for s in all_signals]
        assert "ExitSignal" in reasons
        assert "EntrySignal" not in reasons

        assert bot._market_data_stale_logged is True
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.long_first.max_capital_ars = original_max_capital
        SETTINGS.long_first.max_risk_pct_per_trade = original_risk_pct
        SETTINGS.technical_analysis.enabled = original_ta_enabled
        SETTINGS.risk.max_market_data_staleness_seconds = original_staleness_threshold


def test_on_book_update_uses_book_as_of_not_dispatch_time_for_staleness():
    """
    Regresion del hallazgo del 2026-08-31 (ver docs/AUDITORIA_MAESTRA_2026-08-27.md,
    seguimiento; y RiskConfig.max_market_data_staleness_seconds): antes,
    _on_book_update marcaba self._spot_last_update_at con `datetime.now()`
    (la hora de ESTE despacho), no la hora real del dato. Eso dejaba ciega a
    la guardia de staleness ante BrokerRestSource/IOL, que puede reproducir
    una cotizacion cacheada de hace rato como si fuera nueva en cada poll
    (a diferencia de Data912RestSource, que falla de forma atomica para spot
    Y opciones a la vez - ver docstring de la clase). Ahora se usa
    `book.as_of` (la hora real de origen del dato, ver
    live_shadow_feed.RawQuote.as_of / LiveShadowFeed.poll()), asi que un book
    con un `as_of` viejo debe marcar los datos como stale aunque el
    despacho haya ocurrido en este mismo instante.
    """
    original_shadow = SETTINGS.shadow.enabled
    original_threshold = SETTINGS.risk.max_market_data_staleness_seconds
    SETTINGS.shadow.enabled = True
    SETTINGS.risk.max_market_data_staleness_seconds = 60.0
    try:
        bot = GgalOptionsBot()
        old_as_of = time.time() - 300.0  # 5 minutos de antiguedad REAL del dato
        book = OrderBookSnapshot(
            SETTINGS.instruments.contado_ticker, bid=5199.0, ask=5201.0,
            bid_size=500, ask_size=500, as_of=old_as_of,
        )
        bot._on_book_update(SETTINGS.instruments.contado_ticker, book)

        now = datetime.now(timezone.utc)
        assert bot._is_market_data_stale(now) is True, (
            "con el bug viejo, _spot_last_update_at quedaba en 'ahora' en cada "
            "despacho sin importar la antiguedad real de book.as_of"
        )
        staleness = bot._market_data_staleness_seconds(now)
        assert staleness is not None and staleness >= 299.0
    finally:
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.risk.max_market_data_staleness_seconds = original_threshold


def test_weekly_asymmetric_cycle_excludes_stale_option_quote_from_entry_scan():
    """
    Regresion del hallazgo del 2026-08-31 (ver RiskConfig.
    max_option_quote_staleness_seconds): a diferencia del test de arriba
    (staleness del SPOT completo), este cubre el caso REAL observado en
    produccion el 31/08 (~12:00-14:11 ART) - BrokerRestSource devolviendo
    timeouts repetidos y sostenidos SOLO al refrescar la cadena de opciones,
    mientras el spot se seguia actualizando con normalidad. Sin una guardia
    por-opcion, la guardia de staleness del spot (que sigue fresco) nunca se
    activa, y la opcion candidata quedaria compitiendo por una señal de
    entrada con un precio de hace rato. Mismo fixture base que
    test_weekly_asymmetric_cycle_reconciles_exit_before_sizing_new_entry,
    pero con el book de la candidata ("target") envejecido artificialmente
    por encima del umbral - el spot NO se toca (sigue "fresco"), asi que
    esto aisla especificamente la guardia por-opcion.
    """
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_max_capital = SETTINGS.long_first.max_capital_ars
    original_risk_pct = SETTINGS.long_first.max_risk_pct_per_trade
    original_ta_enabled = SETTINGS.technical_analysis.enabled
    original_option_staleness = SETTINGS.risk.max_option_quote_staleness_seconds
    SETTINGS.strategy.active = "weekly_asymmetric"
    SETTINGS.shadow.enabled = True
    SETTINGS.technical_analysis.enabled = False
    SETTINGS.long_first.max_capital_ars = 55_000.0
    SETTINGS.long_first.max_risk_pct_per_trade = 1.0
    SETTINGS.risk.max_option_quote_staleness_seconds = 90.0
    try:
        bot = GgalOptionsBot()
        spot = 5200.0

        def smile_iv(strike: float) -> float:
            x = math.log(strike / spot)
            return 0.45 + 6.0 * x * x

        # -- Bases de relleno FRESCAS (as_of default = ahora) ------------------
        filler_strikes = [4700, 4900, 5000, 5100, 5300 - 100, 5400, 5500, 5700]
        for k in filler_strikes:
            q = _quote(f"GFGC{k}O", k, smile_iv(k), spot, days_biz=3)
            bot.option_chain.upsert_quote(q)
            bot._recent_volumes[q.symbol] = 1000.0

        # -- Candidata de entrada, PERO con una punta de hace 10 minutos -------
        # (muy por encima de max_option_quote_staleness_seconds=90s): simula
        # exactamente la cadena de opciones caida sola que se vio en el log real.
        stale_as_of = time.time() - 600.0
        target = _quote(
            "GFGC5150O", 5150, smile_iv(5150) - 0.08, spot, days_biz=3,
            greeks={"gamma": 0.01, "vega": 5.0, "delta": 0.5, "theta": -1.0},
            as_of=stale_as_of,
        )
        bot.option_chain.upsert_quote(target)
        bot._recent_volumes[target.symbol] = 1000.0

        # El spot NO se marca stale (se deja _spot_last_update_at en None ->
        # _is_market_data_stale() da False): esto aisla la guardia por-opcion.
        assert bot._spot_last_update_at is None

        all_signals = bot._run_weekly_asymmetric_cycle(spot)

        # La entrada candidata NO deberia abrirse: su propia cotizacion es
        # stale, aunque el resto del ciclo (spot, otras bases) este fresco.
        assert bot._position_quantity("GFGC5150O") == 0
        reasons = [type(s).__name__ for s in all_signals]
        assert "EntrySignal" not in reasons
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.long_first.max_capital_ars = original_max_capital
        SETTINGS.long_first.max_risk_pct_per_trade = original_risk_pct
        SETTINGS.technical_analysis.enabled = original_ta_enabled
        SETTINGS.risk.max_option_quote_staleness_seconds = original_option_staleness


ALL_TESTS = [
    test_default_weekly_asymmetric_wires_strategy_and_position_sizer,
    test_vol_arbitrage_selection_leaves_position_sizer_unset,
    test_invalid_active_strategy_falls_back_to_weekly_asymmetric,
    test_capital_available_ars_reflects_open_and_closed_positions,
    test_weekly_asymmetric_cycle_reconciles_exit_before_sizing_new_entry,
    test_weekly_asymmetric_cycle_skips_entries_and_spreads_when_market_data_stale,
    test_on_book_update_uses_book_as_of_not_dispatch_time_for_staleness,
    test_weekly_asymmetric_cycle_excludes_stale_option_quote_from_entry_scan,
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
