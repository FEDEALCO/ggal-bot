"""
test_shadow_trading.py
========================
Tests de sanity para el modulo de Shadow Trading / Live Replay
(ggal_bot/data/live_shadow_feed.py) y para el modo "Paper Execution" agregado
en execution/order_gateway.py (SETTINGS.shadow.enabled). Todo corre sin red
y sin pyRofex real. Correr con:

    python -m ggal_bot.validation.test_shadow_trading
"""

from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, timedelta

# Debe importarse ANTES que ggal_bot.execution.order_gateway para redirigir
# el CSV de auditoria de shadow trading a un path temporal (ver ese modulo:
# evita contaminar logs/shadow_trades.csv real, bug corregido en la
# auditoria del 2026-08-27, seccion 3.3).
from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionChain
from ggal_bot.data.live_shadow_feed import (
    BrokerRestSource,
    Data912RestSource,
    LiveShadowFeed,
    MockReplaySource,
    PrimaryMarketDataSource,
    RawQuote,
    ShadowDataSource,
    _parse_data912_option_symbol,
)
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.execution.order_gateway import (
    OrderGateway, OrderRequest, OrderSide, OrderStatus, OrderTypeEnum,
)


def test_parse_data912_option_symbol_call_and_put():
    cfg = SETTINGS.instruments
    parsed_call = _parse_data912_option_symbol("GFGC4200AG", cfg.call_prefix, cfg.put_prefix)
    assert parsed_call is not None
    option_type, strike, expiry = parsed_call
    assert option_type is OptionType.CALL
    assert strike == 4200.0
    assert expiry.month == 8  # "AG" = Agosto
    assert expiry.weekday() == 4  # tercer viernes

    parsed_put = _parse_data912_option_symbol("GFGV6800AG", cfg.call_prefix, cfg.put_prefix)
    assert parsed_put is not None
    assert parsed_put[0] is OptionType.PUT
    assert parsed_put[1] == 6800.0


def test_parse_data912_option_symbol_rejects_non_option():
    cfg = SETTINGS.instruments
    assert _parse_data912_option_symbol("GGAL", cfg.call_prefix, cfg.put_prefix) is None
    assert _parse_data912_option_symbol("PAMPC1000AG", cfg.call_prefix, cfg.put_prefix) is None


def test_data912_to_raw_quote_maps_real_schema():
    """Valida el mapeo contra el esquema real confirmado de data912.com."""
    record = {
        "symbol": "GFGV6800AG", "q_bid": 56.0, "px_bid": 178.022, "px_ask": 199.98,
        "q_ask": 10.0, "v": 10493.0, "q_op": 907.0, "c": 180.0, "pct_change": -36.6,
    }
    raw = Data912RestSource._to_raw_quote(record)
    assert raw.symbol == "GFGV6800AG"
    assert raw.bid == 178.022
    assert raw.ask == 199.98
    assert raw.bid_size == 56.0
    assert raw.ask_size == 10.0
    assert raw.last_volume == 10493.0
    assert raw.last_price == 180.0


def test_data912_source_unavailable_without_network_or_requests_reports_false_not_raise():
    """
    Sin red de salida (tipico en este entorno de validacion), is_available()
    debe degradar a False de forma prolija, nunca lanzar - es lo que permite
    que el modo 'auto' caiga a Mock/Replay sin tumbar el arranque del bot.
    """
    source = Data912RestSource()
    result = source.is_available()
    assert isinstance(result, bool)


def test_mock_replay_source_bootstrap_generates_calls_and_puts_both_expiries():
    source = MockReplaySource()
    candidates = source.bootstrap()
    assert len(candidates) > 0

    types = {c[1] for c in candidates}
    assert OptionType.CALL in types and OptionType.PUT in types

    expiries = {c[3] for c in candidates}
    assert len(expiries) == 2  # mock_expiries_days_ahead trae 2 vencimientos por defecto
    for e in expiries:
        assert e > date.today()


def test_mock_replay_source_fetch_snapshot_produces_valid_books():
    source = MockReplaySource()
    source.bootstrap()
    spot_quote, option_quotes = source.fetch_snapshot()

    assert spot_quote is not None
    assert spot_quote.bid > 0 and spot_quote.ask > spot_quote.bid

    assert len(option_quotes) > 0
    for raw in option_quotes.values():
        assert raw.bid > 0
        assert raw.ask > raw.bid  # el generador siempre deja al menos el spread minimo
        assert raw.bid_size > 0 and raw.ask_size > 0


def test_mock_replay_source_is_deterministic_with_fixed_seed():
    """Misma semilla -> misma secuencia de spot, para poder reproducir una corrida de test."""
    import ggal_bot.config as config_module

    original_seed = SETTINGS.shadow.mock_random_seed
    SETTINGS.shadow.mock_random_seed = 42
    try:
        source_a = MockReplaySource()
        source_a.bootstrap()
        spot_a, _ = source_a.fetch_snapshot()

        source_b = MockReplaySource()
        source_b.bootstrap()
        spot_b, _ = source_b.fetch_snapshot()

        assert abs(spot_a.last_price - spot_b.last_price) < 1e-9
    finally:
        SETTINGS.shadow.mock_random_seed = original_seed


def test_live_shadow_feed_forces_mock_source_via_config():
    original_source = SETTINGS.shadow.data_source
    SETTINGS.shadow.data_source = "mock"
    try:
        updates = []
        feed = LiveShadowFeed(on_book_update=lambda sym, book: updates.append((sym, book)))
        assert isinstance(feed._source, MockReplaySource)

        chain = OptionChain()
        tickers = feed.bootstrap_universe(chain)
        assert SETTINGS.instruments.contado_ticker in tickers
        assert len(chain.all_quotes()) > 0

        feed.poll(chain)
        symbols_updated = {u[0] for u in updates}
        assert SETTINGS.instruments.contado_ticker in symbols_updated
        # Al menos alguna opcion del universo generado debe haberse actualizado.
        assert any(q.symbol in symbols_updated for q in chain.all_quotes())
    finally:
        SETTINGS.shadow.data_source = original_source


def test_live_shadow_feed_auto_falls_back_to_mock_without_network():
    """
    En este entorno de validacion no hay red de salida a data912.com, asi
    que el modo 'auto' (default) debe caer a Mock/Replay sin lanzar ni
    dejar el bot sin fuente de datos.
    """
    original_source = SETTINGS.shadow.data_source
    SETTINGS.shadow.data_source = "auto"
    try:
        feed = LiveShadowFeed(on_book_update=lambda *_: None)
        assert isinstance(feed._source, (MockReplaySource, Data912RestSource))
        chain = OptionChain()
        tickers = feed.bootstrap_universe(chain)
        assert len(tickers) >= 1
    finally:
        SETTINGS.shadow.data_source = original_source


# ---------------------------------------------------------------------------
# Multi-fuente con prioridad y failover (ver config.ShadowConfig.source_priority
# / data/live_shadow_feed.py:LiveShadowFeed, PrimaryMarketDataSource,
# BrokerRestSource) - Request de evaluacion de fuentes alternativas de datos.
# ---------------------------------------------------------------------------

def test_shadow_config_source_priority_explicit_list():
    original = SETTINGS.shadow.source_priority_raw
    SETTINGS.shadow.source_priority_raw = "primary_ws, data912 ,mock"
    try:
        assert SETTINGS.shadow.source_priority() == ("primary_ws", "data912", "mock")
    finally:
        SETTINGS.shadow.source_priority_raw = original


def test_shadow_config_source_priority_legacy_fallback():
    """Sin GGAL_BOT_SHADOW_SOURCE_PRIORITY, se debe respetar el selector legado tal cual se comportaba antes."""
    original_raw = SETTINGS.shadow.source_priority_raw
    original_legacy = SETTINGS.shadow.data_source
    SETTINGS.shadow.source_priority_raw = ""
    try:
        SETTINGS.shadow.data_source = "data912"
        assert SETTINGS.shadow.source_priority() == ("data912",)
        SETTINGS.shadow.data_source = "mock"
        assert SETTINGS.shadow.source_priority() == ("mock",)
        SETTINGS.shadow.data_source = "auto"
        assert SETTINGS.shadow.source_priority() == ("data912", "mock")
    finally:
        SETTINGS.shadow.source_priority_raw = original_raw
        SETTINGS.shadow.data_source = original_legacy


def test_primary_market_data_source_unavailable_without_pyrofex_reports_false_not_raise():
    """En este entorno de validacion pyRofex no esta instalado; is_available() debe degradar a False, nunca lanzar."""
    source = PrimaryMarketDataSource()
    assert source.is_available() is False


def test_primary_market_data_source_bootstrap_and_fetch_snapshot_return_empty_when_unavailable():
    source = PrimaryMarketDataSource()
    assert source.bootstrap() == []
    spot, options = source.fetch_snapshot()
    assert spot is None
    assert options == {}


def test_broker_rest_source_unavailable_without_credentials():
    original_username = SETTINGS.broker_rest.username
    original_password = SETTINGS.broker_rest.password
    SETTINGS.broker_rest.username = ""
    SETTINGS.broker_rest.password = ""
    try:
        assert BrokerRestSource().is_available() is False
    finally:
        SETTINGS.broker_rest.username = original_username
        SETTINGS.broker_rest.password = original_password


def test_broker_rest_source_never_fabricates_data_without_credentials_or_network():
    """
    Sin credenciales configuradas (y sin red de salida en este entorno de
    validacion), bootstrap()/fetch_snapshot() deben degradar a vacio -
    nunca inventar datos - incluso si se instancia directamente sin pasar
    por is_available().
    """
    source = BrokerRestSource()
    assert source.bootstrap() == []
    spot, options = source.fetch_snapshot()
    assert spot is None
    assert options == {}


def test_broker_rest_source_login_parses_access_token_and_caches_it():
    """
    El mecanismo de login (POST /token, form-urlencoded, grant_type=password)
    esta CONFIRMADO contra la documentacion oficial de IOL (ver docstring de
    BrokerRestSource) - este test valida el parsing de 'access_token' y que
    no se vuelva a loguear mientras el token siga dentro de la ventana de
    cache (evita gastar un login de mas por poll).
    """
    import ggal_bot.data.live_shadow_feed as mod

    original_username = SETTINGS.broker_rest.username
    original_password = SETTINGS.broker_rest.password
    SETTINGS.broker_rest.username = "test_user"
    SETTINGS.broker_rest.password = "test_pass"
    original_http_request_json = mod.http_request_json
    calls = []

    def fake_http_request_json(method, url, timeout, headers=None, data=None):  # noqa: ARG001
        calls.append((method, url, data))
        return {"access_token": "fake-token-123", "expires_in": 900}

    mod.http_request_json = fake_http_request_json
    try:
        source = mod.BrokerRestSource()
        assert source.is_available() is True
        assert source._token == "fake-token-123"
        assert len(calls) == 1
        method, url, data = calls[0]
        assert method == "POST"
        assert url.endswith("/token")
        assert data["grant_type"] == "password"
        assert data["username"] == "test_user"

        assert source._login() is True  # dentro de la ventana de cache: no debe volver a loguearse
        assert len(calls) == 1
    finally:
        mod.http_request_json = original_http_request_json
        SETTINGS.broker_rest.username = original_username
        SETTINGS.broker_rest.password = original_password


def test_broker_rest_source_parse_option_record_uses_confirmed_iol_schema():
    """
    Esquema real CONFIRMADO contra una cuenta de IOL (ver docstring de
    BrokerRestSource / diagnose_iol_api.py): cada registro de /Opciones trae
    'simbolo', 'tipoOpcion' ("Call"/"Put", directo) y 'fechaVencimiento"
    (ISO, directo) - el strike se extrae del simbolo (convencion BYMA).
    """
    rec = {
        "cotizacion": {"ultimoPrecio": 0.45, "puntas": None},
        "simboloSubyacente": "GGAL", "fechaVencimiento": "2026-09-18T15:30:00",
        "tipoOpcion": "Put", "simbolo": "GFGV4200SE",
        "descripcion": "Put GGAL 4,200.00 Vencimiento: 18/09/2026",
        "pais": "argentina", "mercado": "bcba", "tipo": "OPCIONES", "plazo": "t0",
    }
    parsed = BrokerRestSource._parse_option_record(rec)
    assert parsed is not None
    symbol, option_type, strike, expiry = parsed
    assert symbol == "GFGV4200SE"
    assert option_type is OptionType.PUT
    assert strike == 4200.0
    assert expiry == date(2026, 9, 18)


def test_broker_rest_source_parse_option_record_falls_back_to_symbol_when_semantic_fields_missing():
    """Si 'tipoOpcion'/'fechaVencimiento' faltaran, debe caer al parseo por convencion de simbolo (BYMA)."""
    rec = {"simbolo": "GFGC4400SE"}
    parsed = BrokerRestSource._parse_option_record(rec)
    assert parsed is not None
    symbol, option_type, strike, expiry = parsed
    assert option_type is OptionType.CALL
    assert strike == 4400.0
    assert expiry.month == 9


def test_broker_rest_source_parse_option_record_rejects_record_without_symbol():
    assert BrokerRestSource._parse_option_record({"tipoOpcion": "Call"}) is None
    assert BrokerRestSource._parse_option_record("no es un dict") is None


def test_broker_rest_source_parse_quote_record_matches_confirmed_iol_schema():
    """
    Esquema real CONFIRMADO (ver diagnose_iol_api.py): 'ultimoPrecio' a nivel
    raiz, 'puntas' como lista de niveles (se toma el mejor, puntas[0]).
    'puntas' vacia/None (visto en la corrida de referencia, fuera de rueda)
    debe degradar a bid=ask=0, no fabricar un valor de otro campo.
    """
    rec_with_puntas = {
        "ultimoPrecio": 180.0,
        "puntas": [{"precioCompra": 178.022, "cantidadCompra": 56, "precioVenta": 199.98, "cantidadVenta": 10}],
        "montoOperado": 10493.0,
    }
    raw = BrokerRestSource._parse_quote_record("GFGV6800AG", rec_with_puntas)
    assert raw.bid == 178.022 and raw.ask == 199.98
    assert raw.bid_size == 56 and raw.ask_size == 10
    assert raw.last_price == 180.0
    assert raw.last_volume == 10493.0

    rec_sin_puntas = {"ultimoPrecio": 7070.0, "puntas": [], "montoOperado": 13320723130.0}
    raw2 = BrokerRestSource._parse_quote_record("GGAL", rec_sin_puntas)
    assert raw2.bid == 0.0 and raw2.ask == 0.0
    assert raw2.last_price == 7070.0

    rec_puntas_null = {"ultimoPrecio": 0.45, "puntas": None}
    raw3 = BrokerRestSource._parse_quote_record("GFGV4200SE", rec_puntas_null)
    assert raw3.bid == 0.0 and raw3.ask == 0.0
    assert raw3.last_price == 0.45


def test_broker_rest_source_fetch_snapshot_refreshes_whole_chain_in_one_request():
    """
    Confirmado contra una cuenta real (ver diagnose_iol_api.py): el endpoint
    de opciones trae la cotizacion embebida por opcion ('cotizacion') - un
    solo request basta para refrescar TODA la cadena, sin round-robin ni
    limite de simbolos por poll.
    """
    import ggal_bot.data.live_shadow_feed as mod

    original_login = mod.BrokerRestSource._login
    mod.BrokerRestSource._login = lambda self: True
    original_http_get_json = mod.http_get_json

    calls = []

    def fake_http_get_json(url, timeout, headers=None):  # noqa: ARG001
        calls.append(url)
        if url.endswith("/Cotizacion"):
            return {"ultimoPrecio": 7070.0, "puntas": []}
        assert url.endswith("/Opciones")
        return [
            {
                "cotizacion": {"ultimoPrecio": 0.45, "puntas": [
                    {"precioCompra": 0.40, "cantidadCompra": 100, "precioVenta": 0.50, "cantidadVenta": 100},
                ]},
                "tipoOpcion": "Put", "simbolo": "GFGV4200SE", "fechaVencimiento": "2026-09-18T15:30:00",
            },
            {
                "cotizacion": {"ultimoPrecio": 0.0, "puntas": None},
                "tipoOpcion": "Call", "simbolo": "GFGC4400SE", "fechaVencimiento": "2026-09-18T15:30:00",
            },
        ]

    mod.http_get_json = fake_http_get_json
    try:
        source = mod.BrokerRestSource()
        spot, options = source.fetch_snapshot()
        assert spot is not None and spot.last_price == 7070.0
        assert set(options.keys()) == {"GFGV4200SE", "GFGC4400SE"}
        assert options["GFGV4200SE"].bid == 0.40 and options["GFGV4200SE"].ask == 0.50
        assert len(calls) == 2  # 1 para el subyacente + 1 para TODA la cadena de opciones
    finally:
        mod.http_get_json = original_http_get_json
        mod.BrokerRestSource._login = original_login


def test_broker_rest_source_refreshes_near_the_money_quotes_individually():
    """
    HALLAZGO REAL 2026-09-01 (ver diagnose_iol_puntas.py corrido contra una
    cuenta real en horario de rueda, y el docstring de
    _refresh_near_the_money_quotes()): el endpoint de CADENA nunca trae
    'puntas' pobladas (siempre null, incluso con ultimoPrecio>0) - solo el
    endpoint INDIVIDUAL por simbolo las trae reales. Este test confirma
    que fetch_snapshot() ahora pide individualmente SOLO las opciones
    dentro de la banda de moneyness alrededor del spot (aca: la de strike
    igual al spot), dejando la que esta MUY lejos (strike muy por encima)
    tal cual vino del batch (sin punta) - para no pedir de mas.
    """
    import ggal_bot.data.live_shadow_feed as mod

    original_login = mod.BrokerRestSource._login
    mod.BrokerRestSource._login = lambda self: True
    original_http_get_json = mod.http_get_json
    individual_calls = []
    underlying = SETTINGS.instruments.underlying_symbol

    def fake_http_get_json(url, timeout, headers=None):  # noqa: ARG001
        if url.endswith(f"/{underlying}/Cotizacion"):
            return {"ultimoPrecio": 7000.0, "puntas": []}
        if url.endswith("/Opciones"):
            return [
                {
                    "cotizacion": {"ultimoPrecio": 5.0, "puntas": None},
                    "tipoOpcion": "Call", "simbolo": "GFGC7000SE", "fechaVencimiento": "2026-09-18T15:30:00",
                },
                {
                    "cotizacion": {"ultimoPrecio": 0.0, "puntas": None},
                    "tipoOpcion": "Call", "simbolo": "GFGC20000SE", "fechaVencimiento": "2026-09-18T15:30:00",
                },
            ]
        # Unico caso restante: endpoint INDIVIDUAL por simbolo de OPCION
        # (no el subyacente, no la cadena) - la lejana (20000, muy fuera
        # de la banda de moneyness) jamas deberia llegar aca.
        individual_calls.append(url)
        assert url.endswith("/GFGC7000SE/Cotizacion"), f"pidio de mas una opcion fuera de banda: {url}"
        return {"ultimoPrecio": 5.0, "puntas": [
            {"precioCompra": 4.8, "cantidadCompra": 10, "precioVenta": 5.2, "cantidadVenta": 8},
        ]}

    mod.http_get_json = fake_http_get_json
    try:
        source = mod.BrokerRestSource()
        source.bootstrap()
        spot, options = source.fetch_snapshot()
        assert spot is not None and spot.last_price == 7000.0
        assert len(individual_calls) == 1
        assert options["GFGC7000SE"].bid == 4.8 and options["GFGC7000SE"].ask == 5.2
        # Fuera de banda: se queda con lo que vino del batch (sin punta, tal cual IOL lo devuelve ahi).
        assert options["GFGC20000SE"].bid == 0.0 and options["GFGC20000SE"].ask == 0.0
    finally:
        mod.http_get_json = original_http_get_json
        mod.BrokerRestSource._login = original_login


def test_broker_rest_source_near_the_money_refresh_is_throttled():
    """
    _refresh_near_the_money_quotes() no debe disparar una tanda de
    requests individuales en CADA poll (cada ~2s) - ver
    individual_quote_min_refresh_interval_seconds: un segundo
    fetch_snapshot() inmediato despues del primero NO debe repetir las
    llamadas individuales (se sigue sirviendo lo ya cacheado).
    """
    import ggal_bot.data.live_shadow_feed as mod

    original_login = mod.BrokerRestSource._login
    mod.BrokerRestSource._login = lambda self: True
    original_http_get_json = mod.http_get_json
    individual_calls = []
    underlying = SETTINGS.instruments.underlying_symbol

    def fake_http_get_json(url, timeout, headers=None):  # noqa: ARG001
        if url.endswith(f"/{underlying}/Cotizacion"):
            return {"ultimoPrecio": 7000.0, "puntas": []}
        if url.endswith("/Opciones"):
            return [
                {
                    "cotizacion": {"ultimoPrecio": 5.0, "puntas": None},
                    "tipoOpcion": "Call", "simbolo": "GFGC7000SE", "fechaVencimiento": "2026-09-18T15:30:00",
                },
            ]
        individual_calls.append(url)
        return {"ultimoPrecio": 5.0, "puntas": [
            {"precioCompra": 4.8, "cantidadCompra": 10, "precioVenta": 5.2, "cantidadVenta": 8},
        ]}

    mod.http_get_json = fake_http_get_json
    try:
        source = mod.BrokerRestSource()
        source.bootstrap()
        source.fetch_snapshot()
        assert len(individual_calls) == 1
        source.fetch_snapshot()  # inmediatamente despues: throttle debe impedir un segundo refresh
        assert len(individual_calls) == 1
    finally:
        mod.http_get_json = original_http_get_json
        mod.BrokerRestSource._login = original_login


def test_broker_rest_source_bootstrap_precarga_el_cache_de_cotizaciones():
    """bootstrap() debe aprovechar la 'cotizacion' embebida en /Opciones para precargar el cache, sin esperar a poll()."""
    import ggal_bot.data.live_shadow_feed as mod

    original_login = mod.BrokerRestSource._login
    mod.BrokerRestSource._login = lambda self: True
    original_http_get_json = mod.http_get_json

    def fake_http_get_json(url, timeout, headers=None):  # noqa: ARG001
        assert url.endswith("/Opciones")
        return [
            {
                "cotizacion": {"ultimoPrecio": 0.45, "puntas": []},
                "tipoOpcion": "Put", "simbolo": "GFGV4200SE", "fechaVencimiento": "2026-09-18T15:30:00",
            },
        ]

    mod.http_get_json = fake_http_get_json
    try:
        source = mod.BrokerRestSource()
        candidates = source.bootstrap()
        assert len(candidates) == 1
        assert "GFGV4200SE" in source._quote_cache
        assert source._quote_cache["GFGV4200SE"].last_price == 0.45
    finally:
        mod.http_get_json = original_http_get_json
        mod.BrokerRestSource._login = original_login


def test_live_shadow_feed_respects_explicit_source_priority_order():
    original_raw = SETTINGS.shadow.source_priority_raw
    SETTINGS.shadow.source_priority_raw = "mock,data912"
    try:
        feed = LiveShadowFeed(on_book_update=lambda *_: None)
        assert feed._priority == ("mock", "data912")
        assert isinstance(feed._source, MockReplaySource)  # 'mock' es siempre disponible: gana por ser 1ra en la lista
    finally:
        SETTINGS.shadow.source_priority_raw = original_raw


def test_live_shadow_feed_ignores_unknown_source_name_in_priority():
    original_raw = SETTINGS.shadow.source_priority_raw
    SETTINGS.shadow.source_priority_raw = "nombre_inventado,mock"
    try:
        feed = LiveShadowFeed(on_book_update=lambda *_: None)
        assert isinstance(feed._source, MockReplaySource)
    finally:
        SETTINGS.shadow.source_priority_raw = original_raw


class _AlwaysFailingSource(ShadowDataSource):
    """Doble de prueba: simula una fuente que esta disponible pero cuyo poll() nunca trae datos utiles."""

    def bootstrap(self):
        return []

    def fetch_snapshot(self):
        return None, {}

    def is_available(self):
        return True


class _CachedButSoftFailingSource(ShadowDataSource):
    """
    Doble de prueba (BUG REAL CORREGIDO, ver seguimiento de auditoria del
    2026-09-01): simula el comportamiento real de BrokerRestSource cuando el
    refresh en vivo falla pero el cache sigue sirviendo el ultimo dato
    conocido - a diferencia de _AlwaysFailingSource (que devuelve
    None/vacio), esta fuente SIEMPRE devuelve datos no vacios via
    fetch_snapshot(), pero reporta had_last_fetch_error()==True. Antes del
    fix, LiveShadowFeed.poll() solo miraba "spot_quote is None and not
    option_quotes" para contar fallos, asi que una fuente asi jamas hacia
    avanzar _consecutive_failures pese a fallar en cada poll - exactamente
    el hueco que se vio en produccion (>1 hora de fallos de refresh de la
    cadena de opciones sin que el failover se disparara una sola vez).
    """

    def bootstrap(self):
        return [("GFGC4200SE", OptionType.CALL, 4200.0, date(2026, 9, 18))]

    def fetch_snapshot(self):
        spot = RawQuote(symbol="GGAL", bid=7000.0, ask=7010.0, bid_size=100, ask_size=100)
        option = RawQuote(symbol="GFGC4200SE", bid=0.40, ask=0.50, bid_size=100, ask_size=100)
        return spot, {"GFGC4200SE": option}

    def is_available(self):
        return True

    def had_last_fetch_error(self):
        return True


def test_live_shadow_feed_failover_switches_source_after_consecutive_failures():
    """
    No debe conmutar tras un unico poll fallido (ver
    ShadowConfig.source_failure_threshold - mismo criterio "no overreact a
    un fallo aislado" que la guardia de staleness de datos), pero si tras
    alcanzar el umbral configurado de fallos CONSECUTIVOS.
    """
    original_threshold = SETTINGS.shadow.source_failure_threshold
    SETTINGS.shadow.source_failure_threshold = 2
    try:
        feed = LiveShadowFeed(on_book_update=lambda *_: None)
        feed._priority = ("failing", "mock")
        feed._source = _AlwaysFailingSource()
        feed._source_index = 0
        feed._consecutive_failures = 0
        chain = OptionChain()
        feed._option_chain = chain

        import ggal_bot.data.live_shadow_feed as mod
        original_factories = dict(mod._SOURCE_FACTORIES)
        mod._SOURCE_FACTORIES["failing"] = _AlwaysFailingSource
        try:
            feed.poll(chain)
            assert isinstance(feed._source, _AlwaysFailingSource)  # 1er fallo: todavia no alcanza el umbral

            feed.poll(chain)
            assert isinstance(feed._source, MockReplaySource)  # 2do fallo consecutivo: conmuta
        finally:
            mod._SOURCE_FACTORIES.clear()
            mod._SOURCE_FACTORIES.update(original_factories)
    finally:
        SETTINGS.shadow.source_failure_threshold = original_threshold


def test_live_shadow_feed_failover_counts_soft_errors_even_when_cached_data_keeps_flowing():
    """
    BUG REAL CORREGIDO (ver ShadowDataSource.had_last_fetch_error() y
    seguimiento de auditoria del 2026-09-01): una fuente que sigue
    devolviendo datos cacheados no vacios pero cuyo refresh en vivo esta
    fallando de forma sostenida (el comportamiento real de BrokerRestSource,
    ver _CachedButSoftFailingSource) DEBE hacer avanzar
    _consecutive_failures y disparar el failover al llegar al umbral -
    antes de este fix, "spot_quote is None and not option_quotes" nunca era
    cierto para una fuente asi, y el failover quedaba ciego para siempre.
    """
    original_threshold = SETTINGS.shadow.source_failure_threshold
    SETTINGS.shadow.source_failure_threshold = 2
    try:
        feed = LiveShadowFeed(on_book_update=lambda *_: None)
        feed._priority = ("soft-failing", "mock")
        feed._source = _CachedButSoftFailingSource()
        feed._source_index = 0
        feed._consecutive_failures = 0
        chain = OptionChain()
        feed._option_chain = chain

        import ggal_bot.data.live_shadow_feed as mod
        original_factories = dict(mod._SOURCE_FACTORIES)
        mod._SOURCE_FACTORIES["soft-failing"] = _CachedButSoftFailingSource
        try:
            feed.poll(chain)
            # 1er poll con error suave: todavia no alcanza el umbral, pero
            # los datos cacheados igual se despachan (freeze, no starve).
            assert isinstance(feed._source, _CachedButSoftFailingSource)
            assert feed._consecutive_failures == 1

            feed.poll(chain)
            # 2do error suave consecutivo: alcanza el umbral -> conmuta,
            # pese a que fetch_snapshot() NUNCA devolvio spot/opciones vacios.
            assert isinstance(feed._source, MockReplaySource)
        finally:
            mod._SOURCE_FACTORIES.clear()
            mod._SOURCE_FACTORIES.update(original_factories)
    finally:
        SETTINGS.shadow.source_failure_threshold = original_threshold


def test_broker_rest_source_fetch_snapshot_flags_soft_error_but_keeps_serving_cache():
    """
    BUG REAL CORREGIDO: cuando el refresh en vivo falla (timeout, 500,
    proxy caido), fetch_snapshot() debe seguir devolviendo el ultimo dato
    cacheado (comportamiento pre-existente, "freeze no starve") Y ADEMAS
    marcar had_last_fetch_error()=True para que LiveShadowFeed.poll() pueda
    contarlo como un fallo real, algo que antes era invisible para el
    contador de failover.
    """
    import ggal_bot.data.live_shadow_feed as mod

    original_login = mod.BrokerRestSource._login
    mod.BrokerRestSource._login = lambda self: True
    original_http_get_json = mod.http_get_json

    state = {"fail": False}

    def fake_http_get_json(url, timeout, headers=None):  # noqa: ARG001
        if state["fail"]:
            raise RuntimeError("Read timed out (simulado)")
        if url.endswith("/Cotizacion"):
            return {"ultimoPrecio": 7070.0, "puntas": []}
        return [{
            "cotizacion": {"ultimoPrecio": 0.45, "puntas": [
                {"precioCompra": 0.40, "cantidadCompra": 100, "precioVenta": 0.50, "cantidadVenta": 100},
            ]},
            "tipoOpcion": "Call", "simbolo": "GFGC4200SE", "fechaVencimiento": "2026-09-18T15:30:00",
        }]

    mod.http_get_json = fake_http_get_json
    try:
        source = mod.BrokerRestSource()

        spot, options = source.fetch_snapshot()
        assert spot is not None and "GFGC4200SE" in options
        assert source.had_last_fetch_error() is False

        state["fail"] = True
        spot2, options2 = source.fetch_snapshot()
        # Cache-replay: los datos siguen siendo los mismos de la ultima vez
        # que el refresh funciono, no None/vacio.
        assert spot2 is not None and spot2.last_price == 7070.0
        assert "GFGC4200SE" in options2
        # Pero ahora SI queda marcado el error suave, para que el failover
        # pueda enterarse.
        assert source.had_last_fetch_error() is True
    finally:
        mod.http_get_json = original_http_get_json
        mod.BrokerRestSource._login = original_login


def test_live_shadow_feed_advance_to_next_source_falls_back_to_mock_when_priority_exhausted():
    """
    Red de seguridad final: aunque el usuario arme una prioridad sin 'mock'
    y todas fallen, LiveShadowFeed jamas debe quedar sin ninguna fuente.
    """
    feed = LiveShadowFeed(on_book_update=lambda *_: None)
    feed._priority = ("failing",)  # 'mock' deliberadamente ausente de esta prioridad
    feed._source = _AlwaysFailingSource()
    feed._source_index = 0

    advanced = feed._advance_to_next_source()
    assert advanced is True
    assert isinstance(feed._source, MockReplaySource)


def test_order_gateway_shadow_mode_fills_immediately_at_reference_price():
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        gateway = OrderGateway()
        request = OrderRequest(
            symbol="GFGC5200O", side=OrderSide.BUY, quantity=3, price=101.0,
            order_type=OrderTypeEnum.LIMIT,
        )
        state = gateway.send(request, reference_price=99.5)

        assert state.status is OrderStatus.FILLED
        assert state.filled_quantity == 3
        assert state.avg_fill_price == 99.5  # fill al mid de referencia, no al precio limite
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_order_gateway_shadow_mode_logs_fill_to_audit_csv(tmp_path=None):
    import csv
    import tempfile
    from pathlib import Path
    from ggal_bot.execution.order_gateway import ShadowAuditLogger

    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audit_path = Path(tmp_dir) / "shadow_trades_test.csv"
            gateway = OrderGateway()
            gateway._shadow_logger = ShadowAuditLogger(path=audit_path)

            request = OrderRequest(
                symbol="GFGV4800F", side=OrderSide.SELL, quantity=2, price=50.0,
                order_type=OrderTypeEnum.LIMIT,
            )
            gateway.send(request, reference_price=49.0)

            assert audit_path.exists()
            with open(audit_path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            assert rows[0][0] == "timestamp_utc"  # header
            assert len(rows) == 2  # header + 1 fill
            data_row = rows[1]
            assert data_row[2] == "GFGV4800F"  # symbol
            assert data_row[3] == "sell"        # side
            assert float(data_row[7]) == 49.0   # fill_price == reference_price (mid)
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_order_gateway_shadow_mode_never_touches_real_send_order(monkeypatch=None):
    """
    Regresion critica: en modo shadow, OrderGateway.send() NUNCA debe llamar
    a la funcion send_order() de bajo nivel (que es la que hablaria con
    pyRofex/el ALYC real). Se parchea temporalmente para detectar cualquier
    invocacion inesperada.
    """
    import ggal_bot.execution.order_gateway as gateway_module

    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    calls = []
    original_send_order = gateway_module.send_order
    gateway_module.send_order = lambda *a, **k: (calls.append((a, k)) or {"status": "sent"})
    try:
        gateway = OrderGateway()
        request = OrderRequest(
            symbol="GFGC5200O", side=OrderSide.BUY, quantity=1, price=100.0,
            order_type=OrderTypeEnum.LIMIT,
        )
        gateway.send(request, reference_price=99.5)
        assert calls == []  # send_order() real jamas fue invocado
    finally:
        gateway_module.send_order = original_send_order
        SETTINGS.shadow.enabled = original_enabled


def test_order_gateway_get_account_positions_shadow_mode_reflects_local_fills():
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        gateway = OrderGateway()
        buy = OrderRequest(symbol="GGAL", side=OrderSide.BUY, quantity=100, price=5200.0)
        sell = OrderRequest(symbol="GGAL", side=OrderSide.SELL, quantity=40, price=5205.0)
        gateway.send(buy, reference_price=5200.0)
        gateway.send(sell, reference_price=5205.0)

        result = gateway.get_account_positions()
        assert result.get("mode") == "shadow"
        positions_by_symbol = {p["symbol"]: p["quantity"] for p in result["positions"]}
        assert positions_by_symbol.get("GGAL") == 60  # 100 compradas - 40 vendidas
    finally:
        SETTINGS.shadow.enabled = original_enabled


ALL_TESTS = [
    test_parse_data912_option_symbol_call_and_put,
    test_parse_data912_option_symbol_rejects_non_option,
    test_data912_to_raw_quote_maps_real_schema,
    test_data912_source_unavailable_without_network_or_requests_reports_false_not_raise,
    test_mock_replay_source_bootstrap_generates_calls_and_puts_both_expiries,
    test_mock_replay_source_fetch_snapshot_produces_valid_books,
    test_mock_replay_source_is_deterministic_with_fixed_seed,
    test_live_shadow_feed_forces_mock_source_via_config,
    test_live_shadow_feed_auto_falls_back_to_mock_without_network,
    test_shadow_config_source_priority_explicit_list,
    test_shadow_config_source_priority_legacy_fallback,
    test_primary_market_data_source_unavailable_without_pyrofex_reports_false_not_raise,
    test_primary_market_data_source_bootstrap_and_fetch_snapshot_return_empty_when_unavailable,
    test_broker_rest_source_unavailable_without_credentials,
    test_broker_rest_source_never_fabricates_data_without_credentials_or_network,
    test_broker_rest_source_login_parses_access_token_and_caches_it,
    test_broker_rest_source_parse_option_record_uses_confirmed_iol_schema,
    test_broker_rest_source_parse_option_record_falls_back_to_symbol_when_semantic_fields_missing,
    test_broker_rest_source_parse_option_record_rejects_record_without_symbol,
    test_broker_rest_source_parse_quote_record_matches_confirmed_iol_schema,
    test_broker_rest_source_fetch_snapshot_refreshes_whole_chain_in_one_request,
    test_broker_rest_source_refreshes_near_the_money_quotes_individually,
    test_broker_rest_source_near_the_money_refresh_is_throttled,
    test_broker_rest_source_bootstrap_precarga_el_cache_de_cotizaciones,
    test_live_shadow_feed_respects_explicit_source_priority_order,
    test_live_shadow_feed_ignores_unknown_source_name_in_priority,
    test_live_shadow_feed_failover_switches_source_after_consecutive_failures,
    test_live_shadow_feed_failover_counts_soft_errors_even_when_cached_data_keeps_flowing,
    test_broker_rest_source_fetch_snapshot_flags_soft_error_but_keeps_serving_cache,
    test_live_shadow_feed_advance_to_next_source_falls_back_to_mock_when_priority_exhausted,
    test_order_gateway_shadow_mode_fills_immediately_at_reference_price,
    test_order_gateway_shadow_mode_logs_fill_to_audit_csv,
    test_order_gateway_shadow_mode_never_touches_real_send_order,
    test_order_gateway_get_account_positions_shadow_mode_reflects_local_fills,
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
