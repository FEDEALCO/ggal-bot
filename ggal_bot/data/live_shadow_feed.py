"""
live_shadow_feed.py
====================
Modulo Adapter de "Shadow Trading / Live Replay": permite validar la logica
cuantitativa del bot (IV, griegas, señales de arbitraje, delta-hedging)
contra datos de mercado que se mueven en tiempo (real) sin depender de que
el ambiente REMARKET del ALYC tenga aprovisionada la cadena completa de
opciones de GGAL (ver diagnose_instruments.py: en el caso real reportado,
878 instrumentos y 0 opciones de GGAL en ese ambiente/cuenta).

Inspirado en como plataformas como optionsdesk.com.ar exponen la cadena de
opciones de GGAL en tiempo real: NO se hizo scraping/ingenieria inversa de
ese sitio en particular (no se encontro una API publica y documentada para
el mismo). En su lugar se usa una fuente equivalente y verificada:

    https://data912.com/live/arg_stocks    -> spot de acciones (incluye GGAL)
    https://data912.com/live/arg_options   -> cadena de opciones (GFGC/GFGV)

Es un endpoint REST publico, sin autenticacion, documentado como "free
market data" por el propio sitio (ver tambien el skill "data912" del
repositorio publico gauss314/skills). Si esta fuente no responde (sin red
saliente, caida, o si el paquete `requests` no esta instalado), este modulo
cae automaticamente a un generador Mock/Replay 100% local (sin red) que
simula una cadena de opciones realista via:

    - Spot: movimiento browniano geometrico (GBM) sin drift.
    - Superficie de IV: nivel ATM + curvatura cuadratica en log-moneyness
      (sonrisa) + ruido idiosincratico Ornstein-Uhlenbeck (mean-reverting)
      por strike/vencimiento.
    - Shocks de "mispricing" transitorios y de probabilidad baja, para poder
      validar que VolatilityArbitrageStrategy efectivamente dispara señales.

En AMBOS casos (data912 real o Mock), la traduccion hacia el resto del bot
es identica a la de data/market_data_feed.py: se arma un OrderBookSnapshot
por instrumento y se despacha via el mismo callback `on_book_update` que usa
MarketDataFeed, por lo que el motor de IV/Griegas (ggal_bot/models/) y el de
señales (strategy/vol_arbitrage.py) no necesitan saber si el dato vino de
pyRofex o de este modulo.

Uso tipico (ver run_bot.py, rama SETTINGS.shadow.enabled):

    feed = LiveShadowFeed(on_book_update=bot._on_book_update)
    tickers = feed.bootstrap_universe(bot.option_chain)   # una vez, al arrancar
    feed.subscribe(tickers)                               # no-op informativo
    ...
    feed.poll(bot.option_chain)                           # una vez por ciclo
"""

from __future__ import annotations

import concurrent.futures
import logging
import math
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from ggal_bot.config import SETTINGS
from ggal_bot.data.http_utils import http_get_json, http_request_json
from ggal_bot.data.option_chain import OptionChain, OptionQuote, OrderBookSnapshot
from ggal_bot.data.market_data_feed import (
    MarketDataFeed,
    _MONTH_LETTER_MAP,
    _business_days_between,
    _third_friday_on_or_after,
    _to_float,
)
from ggal_bot.execution import order_gateway
from ggal_bot.execution.order_gateway import WebSocketConnectionManager
from ggal_bot.models.black_scholes import BlackScholesGreeks, OptionType

logger = logging.getLogger("ggal_bot.live_shadow_feed")

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    requests = None
    _REQUESTS_AVAILABLE = False
    logger.warning(
        "El paquete 'requests' no esta instalado (ver requirements.txt); la "
        "fuente data912.com no estara disponible y el modo Shadow caera "
        "automaticamente al generador Mock/Replay."
    )

# A=Enero ... L=Diciembre invertido, para poder armar simbolos de opcion
# sinteticos (Mock/Replay) a partir de un mes calendario. Reutiliza el mismo
# mapa que ya usa market_data_feed.py para no introducir una segunda
# convencion de letras dentro del proyecto.
_LETTER_MONTH_MAP = {v: k for k, v in _MONTH_LETTER_MAP.items()}

# data912.com nombra las opciones como <PREFIJO><STRIKE><MES-2-LETRAS> (ej.
# "GFGC4200AG" = call, strike 4200, Agosto). Esta es una convencion DISTINTA
# a la de una sola letra (tipo OCC) que asume el fallback de
# market_data_feed._parse_option_symbol - se mantiene separada aca a
# proposito, en vez de reusar ese parser, para no romper ese fallback si el
# ALYC real usa otra convencion. AJUSTAR este mapa si data912 (u otra fuente
# equivalente) cambia de convencion.
_SPANISH_MONTH_CODES = {
    "EN": 1, "FE": 2, "MA": 3, "AB": 4, "MY": 5, "JU": 6,
    "JL": 7, "AG": 8, "SE": 9, "OC": 10, "NO": 11, "DI": 12,
}
_DATA912_OPTION_SYMBOL_RE = re.compile(r"^([A-Z]+)(\d+)([A-Z]{2})$")


@dataclass
class RawQuote:
    """
    Punta de mercado normalizada tal como la entrega la fuente shadow (antes
    de convertirse en OrderBookSnapshot).

    `as_of` (BUG REAL CORREGIDO, ver docs/AUDITORIA_MAESTRA_2026-08-27.md,
    seguimiento del 2026-08-31 y el docstring de
    data/option_chain.py:OrderBookSnapshot.as_of): timestamp Unix de cuando
    se PARSEO este dato desde la fuente. Gracias al `default_factory`, cada
    `RawQuote(...)` nuevo se sella con la hora real de creacion - y como
    BrokerRestSource._quote_cache solo REASIGNA una entrada cuando el parseo
    de ese poll fue exitoso (ver fetch_snapshot() abajo), un simbolo que
    fallo repetidas veces sigue devolviendo el MISMO objeto RawQuote de la
    ultima vez que si funciono, con su `as_of` original intacto - exactamente
    la señal que hacia falta para poder distinguir "dato recien confirmado"
    de "cache viejo reproducido de nuevo".
    """
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    last_volume: float = 0.0
    last_price: float = 0.0
    as_of: float = field(default_factory=time.time)


def _parse_data912_option_symbol(
    symbol: str, call_prefix: str, put_prefix: str,
) -> Optional[Tuple[OptionType, float, date]]:
    """Parsea un simbolo estilo data912 (ej. 'GFGC4200AG') en (tipo, strike, vencimiento)."""
    match = _DATA912_OPTION_SYMBOL_RE.match(symbol.upper())
    if not match:
        return None
    prefix, strike_digits, month_code = match.groups()
    if prefix == call_prefix.upper():
        option_type = OptionType.CALL
    elif prefix == put_prefix.upper():
        option_type = OptionType.PUT
    else:
        return None
    month = _SPANISH_MONTH_CODES.get(month_code)
    if month is None:
        return None
    try:
        strike = float(strike_digits)
    except ValueError:
        return None
    expiry = _third_friday_on_or_after(date.today(), month)
    return option_type, strike, expiry


# ---------------------------------------------------------------------------
# Fuentes de datos shadow: interfaz comun
# ---------------------------------------------------------------------------

class ShadowDataSource:
    """
    Interfaz que deben cumplir las fuentes de datos del modo Shadow. Se
    define como clase base simple (sin abc.ABC) para mantener el estilo del
    resto del proyecto; los metodos deben ser sobreescritos por subclases.
    """

    def bootstrap(self) -> List[Tuple[str, OptionType, float, date]]:
        """Devuelve el universo de opciones disponible: lista de (simbolo, tipo, strike, vencimiento)."""
        raise NotImplementedError

    def fetch_snapshot(self) -> Tuple[Optional[RawQuote], Dict[str, RawQuote]]:
        """Devuelve (punta del subyacente o None, {simbolo: punta} para las opciones)."""
        raise NotImplementedError

    def is_available(self) -> bool:
        """
        Probe de disponibilidad usado por el selector multi-fuente (ver
        LiveShadowFeed._instantiate_first_available/_advance_to_next_source):
        debe degradar a False de forma prolija ante cualquier problema
        (nunca lanzar) para que el failover pueda seguir probando el resto
        de la prioridad configurada sin tumbar el proceso. Default: siempre
        disponible (apropiado para fuentes 100 por ciento locales como
        MockReplaySource; las fuentes que dependen de red/credenciales
        sobreescriben esto con un probe real).
        """
        return True

    def had_last_fetch_error(self) -> bool:
        """
        Señaliza si el ULTIMO fetch_snapshot() tuvo algun error de refresh
        que "spot_quote is None and not option_quotes" NO puede detectar
        (BUG REAL CORREGIDO, ver seguimiento de la auditoria y el incidente
        de proxy/timeouts de 2026-09-01 en produccion: LiveShadowFeed.poll()
        solo contaba como fallo un poll con AMBOS spot y opciones vacios,
        pero BrokerRestSource sigue devolviendo el ultimo valor cacheado de
        _quote_cache cuando el refresh en vivo falla - ver su docstring y el
        de RawQuote.as_of - asi que un refresh de la cadena de opciones que
        falla de forma sostenida nunca hacia avanzar _consecutive_failures y
        el failover a la siguiente fuente en source_priority() jamas se
        disparaba pese a fallar durante mas de una hora seguida).
        Default: False (fuentes que no tienen este modo de fallo parcial,
        como MockReplaySource, no necesitan sobreescribir esto).
        """
        return False

    def subscribe(self, tickers: List[str]) -> None:
        """
        Hook opcional para fuentes que necesitan una suscripcion real (ej.
        PrimaryMarketDataSource, que debe llamar a
        pyRofex.market_data_subscription()). Default: no-op, apropiado para
        fuentes basadas en polling (Data912RestSource, MockReplaySource),
        donde poll() ya cumple ese rol en cada ciclo.
        """
        return None


class Data912RestSource(ShadowDataSource):
    """
    Conector de solo lectura (polling REST de alta frecuencia, sin
    autenticacion) contra data912.com. No envia ordenes ni requiere
    credenciales de ALYC: es exclusivamente una fuente de datos para
    Shadow Trading.
    """

    def __init__(self):
        self._cfg = SETTINGS.shadow

    def _get(self, endpoint: str):
        # Timeout de PARED REAL via http_utils.http_get_json (ver docstring
        # de ese modulo): un ConnectTimeoutError/ReadTimeoutError de
        # `requests` con timeout=5.0 en el mensaje puede, en la practica,
        # tardar varios MINUTOS en levantarse en Windows (resolucion DNS
        # colgada a nivel de sistema operativo, no cubierta por el timeout
        # de requests) - eso bloqueaba el ciclo entero de run_bot.py, no
        # solo este poll puntual. Con este wrapper, la espera real nunca
        # supera timeout + margen, sin importar cuanto tarde la llamada
        # subyacente en resolverse por su cuenta.
        if not _REQUESTS_AVAILABLE:
            raise RuntimeError("El paquete 'requests' no esta instalado.")
        url = self._cfg.data912_base_url.rstrip("/") + endpoint
        return http_get_json(url, timeout=self._cfg.request_timeout_seconds)

    def is_available(self) -> bool:
        """Probe liviano para el modo 'auto': confirma que la fuente responde antes de usarla."""
        try:
            stocks = self._get(self._cfg.data912_stocks_endpoint)
            return bool(stocks)
        except Exception as exc:  # noqa: BLE001 - probe deliberadamente permisivo
            logger.debug("Data912RestSource.is_available(): probe fallo: %s", exc)
            return False

    def bootstrap(self) -> List[Tuple[str, OptionType, float, date]]:
        cfg_i = SETTINGS.instruments
        try:
            options = self._get(self._cfg.data912_options_endpoint)
        except Exception as exc:
            logger.warning("Data912RestSource.bootstrap(): fallo al listar opciones (%s).", exc)
            return []

        candidates: List[Tuple[str, OptionType, float, date]] = []
        for rec in options or []:
            symbol = str(rec.get("symbol") or "")
            if not symbol:
                continue
            parsed = _parse_data912_option_symbol(symbol, cfg_i.call_prefix, cfg_i.put_prefix)
            if parsed is None:
                continue
            option_type, strike, expiry = parsed
            candidates.append((symbol, option_type, strike, expiry))

        logger.info("Data912RestSource.bootstrap(): %d opciones de GGAL identificadas (de %d instrumentos recibidos).",
                    len(candidates), len(options or []))
        return candidates

    @staticmethod
    def _to_raw_quote(rec: Dict) -> RawQuote:
        # Esquema confirmado de data912 (/live/arg_stocks y /live/arg_options):
        # symbol, q_bid, px_bid, px_ask, q_ask, v, q_op, c, pct_change.
        return RawQuote(
            symbol=str(rec.get("symbol") or ""),
            bid=_to_float(rec.get("px_bid")),
            ask=_to_float(rec.get("px_ask")),
            bid_size=_to_float(rec.get("q_bid")),
            ask_size=_to_float(rec.get("q_ask")),
            last_volume=_to_float(rec.get("v")),
            last_price=_to_float(rec.get("c")),
        )

    def fetch_snapshot(self) -> Tuple[Optional[RawQuote], Dict[str, RawQuote]]:
        try:
            stocks = self._get(self._cfg.data912_stocks_endpoint)
            options = self._get(self._cfg.data912_options_endpoint)
        except Exception as exc:
            logger.warning("Data912RestSource.fetch_snapshot(): fallo al obtener datos (%s); se omite este poll.", exc)
            return None, {}

        underlying = SETTINGS.instruments.underlying_symbol.upper()
        spot_quote = None
        for rec in stocks or []:
            if str(rec.get("symbol") or "").upper() == underlying:
                spot_quote = self._to_raw_quote(rec)
                break

        option_quotes: Dict[str, RawQuote] = {}
        for rec in options or []:
            raw = self._to_raw_quote(rec)
            if raw.symbol:
                option_quotes[raw.symbol] = raw

        return spot_quote, option_quotes


class MockReplaySource(ShadowDataSource):
    """
    Generador sintetico 100% local (sin red) de una cadena de opciones de
    GGAL con micro-variaciones realistas, para poder probar la logica
    cuantitativa (IV, griegas, señales) cuando no hay credenciales de ALYC
    ni conectividad a una fuente real. Usa el mismo pricer Black-Scholes que
    el resto del bot (ggal_bot/models/black_scholes.py) para que los precios
    generados sean consistentes con como el motor de IV los va a re-leer.
    """

    def __init__(self):
        cfg = SETTINGS.shadow
        self._cfg = cfg
        self._rng = random.Random(cfg.mock_random_seed) if cfg.mock_random_seed else random.Random()
        self._spot = cfg.mock_initial_spot
        self._universe: List[Tuple[str, OptionType, float, date]] = []
        # Estado de la sonrisa por (strike, vencimiento): comun a call y put
        # de la misma base (misma vol implicita via paridad put-call).
        self._sigma_state: Dict[Tuple[float, date], Dict[str, float]] = {}
        self._volume_accum: Dict[str, float] = {}

    def bootstrap(self) -> List[Tuple[str, OptionType, float, date]]:
        cfg = self._cfg
        cfg_i = SETTINGS.instruments
        today = date.today()

        expiries = sorted({today + timedelta(days=int(d)) for d in cfg.mock_expiries_days_ahead})
        strikes = [
            round(cfg.mock_initial_spot + n * cfg.mock_strike_step, 2)
            for n in range(-cfg.mock_num_strikes_each_side, cfg.mock_num_strikes_each_side + 1)
        ]
        strikes = [s for s in strikes if s > 0]

        candidates: List[Tuple[str, OptionType, float, date]] = []
        for expiry in expiries:
            month_letter = _LETTER_MONTH_MAP.get(expiry.month, "A")
            for strike in strikes:
                strike_label = int(strike) if float(strike).is_integer() else strike
                call_symbol = f"{cfg_i.call_prefix}{strike_label}{month_letter}"
                put_symbol = f"{cfg_i.put_prefix}{strike_label}{month_letter}"
                candidates.append((call_symbol, OptionType.CALL, float(strike), expiry))
                candidates.append((put_symbol, OptionType.PUT, float(strike), expiry))
                self._sigma_state.setdefault(
                    (strike, expiry), {"noise": 0.0, "shock": 0.0, "shock_ticks_left": 0},
                )

        self._universe = candidates
        logger.info(
            "MockReplaySource.bootstrap(): %d opciones sinteticas generadas en %d vencimiento(s) (%s).",
            len(candidates), len(expiries), ", ".join(e.isoformat() for e in expiries),
        )
        return candidates

    def fetch_snapshot(self) -> Tuple[Optional[RawQuote], Dict[str, RawQuote]]:
        cfg = self._cfg
        rate = SETTINGS.rate.default_annual_rate
        dividend_yield = SETTINGS.rate.dividend_yield
        today = date.today()

        # --- 1. Un tick de GBM sin drift para el spot. Se usa la vol ATM
        # configurada como proxy de la volatilidad realizada del subyacente,
        # para que el mock sea internamente consistente con la superficie
        # que a la vez alimenta el pricer de las opciones. ---
        dt = max(cfg.poll_interval_seconds, 0.001) / cfg.trading_seconds_per_year
        z = self._rng.gauss(0.0, 1.0)
        underlying_vol = cfg.mock_atm_iv
        self._spot *= math.exp(-0.5 * underlying_vol ** 2 * dt + underlying_vol * math.sqrt(dt) * z)
        self._spot = max(self._spot, cfg.mock_tick_size_underlying)

        spot_half_spread = max(cfg.mock_tick_size_underlying / 2.0, self._spot * 0.0005)
        spot_quote = RawQuote(
            symbol=SETTINGS.instruments.underlying_symbol,
            bid=self._spot - spot_half_spread, ask=self._spot + spot_half_spread,
            bid_size=500.0, ask_size=500.0,
            last_volume=self._rng.uniform(1000.0, 5000.0), last_price=self._spot,
        )

        option_quotes: Dict[str, RawQuote] = {}
        for symbol, option_type, strike, expiry in self._universe:
            state = self._sigma_state[(strike, expiry)]

            # --- 2. Ruido idiosincratico Ornstein-Uhlenbeck discreto (mean-
            # reverting): evita que la sonrisa diverja con el tiempo, a
            # diferencia de un random walk puro. ---
            state["noise"] = state["noise"] * cfg.mock_iv_noise_decay + self._rng.gauss(0.0, cfg.mock_iv_noise_std)

            # --- 3. Shocks de "mispricing" transitorios de baja
            # probabilidad, para poder validar que el motor de señales
            # (strategy/vol_arbitrage.py) efectivamente los detecta. Decaen
            # linealmente durante mock_mispricing_duration_ticks en vez de
            # cortar de golpe, para no generar un salto discontinuo de IV. ---
            if state["shock_ticks_left"] <= 0:
                if self._rng.random() < cfg.mock_mispricing_probability:
                    state["shock_ticks_left"] = cfg.mock_mispricing_duration_ticks
                    state["shock"] = self._rng.choice([-1.0, 1.0]) * (cfg.mock_mispricing_vol_points / 100.0)
            else:
                state["shock_ticks_left"] -= 1
                if state["shock_ticks_left"] <= 0:
                    state["shock"] = 0.0

            decay_factor = (
                state["shock_ticks_left"] / cfg.mock_mispricing_duration_ticks
                if cfg.mock_mispricing_duration_ticks else 0.0
            )
            log_moneyness = math.log(strike / self._spot) if self._spot > 0 else 0.0
            sigma = (
                cfg.mock_atm_iv
                + cfg.mock_smile_curvature * (log_moneyness ** 2)
                + state["noise"]
                + state["shock"] * decay_factor
            )
            sigma = max(sigma, 0.03)  # piso de sanidad: evita sigma<=0 en el pricer

            days_cal = max((expiry - today).days, 1)
            days_biz = max(_business_days_between(today, expiry), 1)
            bs = BlackScholesGreeks(
                spot=self._spot, strike=strike, rate=rate, dividend_yield=dividend_yield,
                days_calendar=days_cal, days_business=days_biz, option_type=option_type,
            )
            try:
                fair = bs.price(sigma)
            except ValueError:
                continue  # sigma/t_vol invalido en este tick puntual: se omite, no se cae el poll

            spread_relative = cfg.mock_atm_spread_pct + cfg.mock_spread_widening_per_logmoneyness * abs(log_moneyness)
            half_spread = max(fair * spread_relative / 2.0, cfg.mock_min_absolute_spread / 2.0)
            bid = max(fair - half_spread, 0.01)
            ask = max(fair + half_spread, bid + 0.01)
            size = self._rng.uniform(cfg.mock_min_size, cfg.mock_max_size)
            self._volume_accum[symbol] = self._volume_accum.get(symbol, 0.0) + self._rng.uniform(0.0, size / 5.0)

            option_quotes[symbol] = RawQuote(
                symbol=symbol, bid=bid, ask=ask, bid_size=size, ask_size=size,
                last_volume=self._volume_accum[symbol], last_price=fair,
            )

        return spot_quote, option_quotes


class PrimaryMarketDataSource(ShadowDataSource):
    """
    Fuente Shadow que reusa la conexion REAL de Primary/Matba Rofex (via
    pyRofex) SOLO para leer market data - nunca para operar (en modo Shadow,
    OrderGateway.send() ya intercepta cualquier orden ANTES de que llegue a
    pyRofex; ver execution/order_gateway.py). Es la opcion mas solida de las
    evaluadas para reemplazar/complementar a data912.com (ver README, seccion
    "Evaluacion de fuentes de datos alternativas"): oficial, documentada, y
    reusa integramente `MarketDataFeed` + `WebSocketConnectionManager`
    (data/market_data_feed.py, execution/order_gateway.py) - el mismo camino
    ya usado (y probado) por el modo de trading real de este proyecto. No se
    fabrica ningun protocolo nuevo ni se hace ingenieria inversa de nada:
    Primary/Matba Rofex vende exactamente este acceso (incluyendo variantes
    de "solo Market Data") a traves de los ALYCs conectados a su red.

    Bridging push -> pull: `MarketDataFeed.handle_market_data()` es un
    callback ASINCRONICO que dispara el websocket de pyRofex en su propio
    hilo interno; `fetch_snapshot()`, en cambio, es SINCRONICO (se llama una
    vez por ciclo desde `LiveShadowFeed.poll()` / `run_bot.py`). Este
    adapter resuelve la diferencia con un cache interno protegido por lock:
    el callback (`_cache_book_update`) solo ESCRIBE en el cache (rapido, sin
    bloquear al hilo del websocket), y `fetch_snapshot()` LEE una copia
    consistente del cache en el instante en que se lo llama - el mismo
    patron de "escritura por callback / lectura sincronica por ciclo" que ya
    usa run_bot.py entre `_on_book_update` y `recompute_cycle()`, solo que
    aca vive adentro de la fuente en vez de en el bot.

    Conexion perezosa (lazy) y NO bloqueante para el resto del selector: no
    se conecta a pyRofex en __init__, sino recien en `is_available()`/
    `bootstrap()` (la primera vez que alguno se llama) - esto permite que
    `LiveShadowFeed` la instancie solo para *probar* disponibilidad (ver
    `_instantiate_first_available`) sin abrir un websocket real hasta
    confirmar que efectivamente es la fuente que se va a usar. Cualquier
    fallo (pyRofex no instalado, credenciales MD-only incompletas, error de
    red/autenticacion) degrada a False/vacio de forma prolija - nunca lanza -
    para que el failover pueda seguir probando el resto de
    ShadowConfig.source_priority() sin interrumpir el proceso.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._book_cache: Dict[str, RawQuote] = {}
        self._last_message_at: Optional[float] = None
        self._feed = MarketDataFeed(on_book_update=self._cache_book_update)
        self._ws_manager: Optional[WebSocketConnectionManager] = None
        self._tickers: List[str] = []
        self._subscribed = False
        self._connect_attempted = False
        self._connect_ok = False

    def _cache_book_update(self, symbol: str, book: OrderBookSnapshot) -> None:
        with self._lock:
            self._book_cache[symbol] = RawQuote(
                symbol=symbol, bid=book.bid, ask=book.ask,
                bid_size=book.bid_size, ask_size=book.ask_size,
                last_volume=book.last_volume,
            )
            self._last_message_at = time.time()

    def _ensure_connected(self) -> bool:
        """
        Conecta (una unica vez por instancia/proceso) contra Primary via
        pyRofex, usando las credenciales MD-only resueltas por
        BrokerConfig.md_credentials(). Idempotente: llamadas posteriores con
        la conexion ya intentada devuelven el resultado cacheado de
        inmediato (no reintenta en cada poll - eso es responsabilidad de
        WebSocketConnectionManager, que ya maneja su propio backoff de
        reconexion una vez que el primer connect() fue exitoso).
        """
        if self._connect_attempted:
            return self._connect_ok
        self._connect_attempted = True

        if not order_gateway._PYROFEX_AVAILABLE:
            logger.warning("PrimaryMarketDataSource: pyRofex no esta instalado; fuente no disponible.")
            return False

        broker = SETTINGS.broker
        ok, msg = broker.validate_md()
        if not ok:
            logger.warning("PrimaryMarketDataSource: %s", msg)
            return False

        user, password, account, environment = broker.md_credentials()
        try:
            import pyRofex
            env = pyRofex.Environment.LIVE if environment == "LIVE" else pyRofex.Environment.REMARKET
            pyRofex.initialize(user=user, password=password, account=account, environment=env)
        except Exception as exc:  # noqa: BLE001 - cualquier fallo de conexion degrada a fuente no disponible
            logger.warning("PrimaryMarketDataSource: fallo pyRofex.initialize() (%s).", exc)
            return False

        try:
            self._ws_manager = WebSocketConnectionManager(
                market_data_handler=self._feed.handle_market_data,
                order_report_handler=lambda _msg: None,  # esta fuente nunca opera, solo lee market data
                on_reconnect=lambda: self._feed.subscribe(self._tickers) if self._tickers else None,
            )
            if not self._ws_manager.connect():
                logger.warning("PrimaryMarketDataSource: no se pudo abrir el websocket de pyRofex.")
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("PrimaryMarketDataSource: excepcion al abrir el websocket (%s).", exc)
            return False

        self._connect_ok = True
        logger.info(
            "PrimaryMarketDataSource: conectado a Primary/Matba Rofex (ambiente=%s, MD-only, "
            "usuario=%s).", environment, user,
        )
        return True

    def is_available(self) -> bool:
        try:
            return self._ensure_connected()
        except Exception as exc:  # noqa: BLE001 - probe deliberadamente permisivo, ver docstring de la clase
            logger.warning("PrimaryMarketDataSource.is_available(): excepcion inesperada (%s).", exc)
            return False

    def bootstrap(self) -> List[Tuple[str, OptionType, float, date]]:
        if not self._ensure_connected():
            return []

        instruments = self._feed._fetch_instruments()
        if not instruments:
            logger.warning("PrimaryMarketDataSource.bootstrap(): no se pudo obtener el listado de instrumentos.")
            return []

        # Reusa la MISMA logica de clasificacion (semantica via
        # underlying/cficode, con fallback a prefijo de simbolo) que ya usa
        # y prueba el camino de trading real (MarketDataFeed.bootstrap_universe),
        # en vez de reimplementar el parsing de instrumentos por segunda vez.
        candidates: List[Tuple[str, OptionType, float, date]] = []
        for inst in instruments:
            symbol = self._feed._extract_symbol(inst)
            if not symbol:
                continue
            classification = self._feed._classify_option(inst, symbol)
            if classification is None:
                continue
            option_type, bare_for_fallback, prefix_for_fallback = classification
            parsed = self._feed._resolve_strike_and_expiry(inst, bare_for_fallback, prefix_for_fallback)
            if parsed is None:
                continue
            strike, expiry = parsed
            candidates.append((symbol, option_type, strike, expiry))

        logger.info(
            "PrimaryMarketDataSource.bootstrap(): %d opciones de GGAL identificadas (de %d instrumentos recibidos).",
            len(candidates), len(instruments),
        )
        return candidates

    def subscribe(self, tickers: List[str]) -> None:
        """
        A diferencia de Data912RestSource/MockReplaySource (donde poll()
        cumple el rol de "suscripcion"), esta fuente SI necesita una
        suscripcion real de websocket para empezar a recibir datos. Se
        guarda contra `tickers == self._tickers` para no volver a llamar a
        pyRofex.market_data_subscription() innecesariamente si se invoca mas
        de una vez con la misma lista (ej. tras un bootstrap redundante).
        """
        if not self._connect_ok:
            logger.warning("PrimaryMarketDataSource.subscribe(): llamado sin conexion activa; se ignora.")
            return
        if self._subscribed and tickers == self._tickers:
            return
        self._tickers = list(tickers)
        try:
            self._feed.subscribe(self._tickers)
            self._subscribed = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("PrimaryMarketDataSource.subscribe(): fallo la suscripcion (%s).", exc)

    def fetch_snapshot(self) -> Tuple[Optional[RawQuote], Dict[str, RawQuote]]:
        if not self._connect_ok:
            return None, {}
        with self._lock:
            snapshot = dict(self._book_cache)

        cfg_i = SETTINGS.instruments
        spot_quote = snapshot.pop(cfg_i.contado_ticker, None)
        if spot_quote is None and cfg_i.futuro_ticker:
            spot_quote = snapshot.pop(cfg_i.futuro_ticker, None)
        return spot_quote, snapshot


# Pool DEDICADO (separado del pool compartido de http_utils, de solo 4
# workers - ver docstring de ese modulo) para las puntas INDIVIDUALES por
# opcion que refresca BrokerRestSource._refresh_near_the_money_quotes():
# ese pool compartido esta dimensionado para "un par de llamadas por poll"
# (spot + cadena batch), no para las hasta ~30 llamadas en paralelo que
# puede pedir un refresh de puntas cercanas al spot - usar el mismo pool
# para ambas cosas dejaria a las llamadas CRITICAS (spot/cadena de CADA
# poll) esperando cola detras de este refresco, mas lento e infrecuente.
_INDIVIDUAL_QUOTE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=6, thread_name_prefix="ggal-bot-iol-quote",
)


class BrokerRestSource(ShadowDataSource):
    """
    Fuente REST del broker IOL/InvertirOnline (https://www.invertironline.com).
    A diferencia de la version anterior de esta clase (scaffold basado en
    documentacion de terceros), el esquema real quedo CONFIRMADO corriendo
    `diagnose_iol_api.py` contra una cuenta real (ver README, seccion "IOL /
    InvertirOnline"):

        - Login (`POST /token`, form-urlencoded, `grant_type=password` +
          `username` + `password` -> `{"access_token": ..., "expires_in": 1200, ...}`):
          confirmado, tanto por la documentacion oficial como por una
          respuesta real.
        - Cotizacion de un titulo (`GET /api/v2/{market}/Titulos/{simbolo}/Cotizacion`):
          confirmado - `ultimoPrecio` a nivel raiz; `puntas` puede venir
          vacia (`[]`) fuera de rueda/sin operaciones recientes (no es un
          error de parsing, es que no hay punta vigente en ese momento).
        - Cadena de opciones (`GET /api/v2/{market}/Titulos/{simbolo}/Opciones`):
          confirmado - cada registro trae `simbolo`, `tipoOpcion` ("Call"/
          "Put", DIRECTO, no hace falta inferirlo del simbolo),
          `fechaVencimiento` (ISO, DIRECTO, no hace falta la heuristica de
          "tercer viernes") y, ademas, una `cotizacion` embebida por opcion
          con el MISMO esquema que el endpoint de Cotizacion individual -
          es decir, UN SOLO request a este endpoint trae la cotizacion de
          TODA la cadena a la vez (174 opciones en la corrida de referencia),
          sin necesidad de un request por simbolo ni de ningun mecanismo de
          rate-limit/round-robin.

    El strike SI sigue sin venir como campo numerico separado - se extrae
    del simbolo (convencion de EXCHANGE, no de este broker: prefijo GFGC/GFGV
    + digitos de strike + mes, ej. "GFGV4200SE" -> 4200.0), la misma que ya
    usa Data912RestSource. tipoOpcion/fechaVencimiento se usan como fuente
    PRIMARIA (semantica, mas confiable) y el simbolo como fallback si
    llegaran a faltar.

    Recomendacion pendiente para el usuario: la corrida de referencia de
    `diagnose_iol_api.py` se hizo fuera de rueda (spot con `puntas: []` y
    varias opciones con `cantidadOperaciones: 0`) - conviene re-confirmar
    con el mercado abierto (~11-17hs ART, dias habiles) que las puntas
    vienen pobladas antes de operar con esta fuente como unica referencia.
    """

    def __init__(self):
        self._cfg = SETTINGS.broker_rest
        self._token: Optional[str] = None
        self._token_obtained_at: Optional[float] = None
        self._universe: List[Tuple[str, OptionType, float, date]] = []
        self._quote_cache: Dict[str, RawQuote] = {}
        # Ver had_last_fetch_error(): flag que distingue "fetch_snapshot()
        # tuvo que resignarse al cache" de "el refresh en vivo funciono".
        self._last_fetch_had_error: bool = False
        # Ver _refresh_near_the_money_quotes(): throttle propio, mas lento
        # que el poll principal (2s) - las puntas de opciones no necesitan
        # refrescarse tan seguido, y esto acota cuantas veces por minuto se
        # dispara una tanda de requests individuales contra IOL.
        self._last_individual_quote_refresh_at: float = 0.0

    def had_last_fetch_error(self) -> bool:
        return self._last_fetch_had_error

    def is_available(self) -> bool:
        if not self._cfg.username or not self._cfg.password:
            logger.debug(
                "BrokerRestSource: BROKER_REST_USERNAME/PASSWORD no configurados; fuente no disponible."
            )
            return False
        try:
            return self._login()
        except Exception as exc:  # noqa: BLE001 - probe deliberadamente permisivo
            logger.warning("BrokerRestSource: fallo el login (%s).", exc)
            return False

    def _login(self) -> bool:
        """
        POST /token (form-urlencoded), grant_type=password + username +
        password -> {"access_token": ..., "expires_in": 1200, ...} -
        CONFIRMADO contra una cuenta real (ver docstring de la clase). Se
        renueva de forma perezosa (10 min de margen bajo los ~20 min de
        `expires_in` observados) en vez de esperar un 401 real, para no
        perder un poll entero por un token vencido.
        """
        if self._token is not None and self._token_obtained_at is not None:
            if time.time() - self._token_obtained_at < 600.0:
                return True

        url = self._cfg.base_url.rstrip("/") + "/token"
        payload = {"username": self._cfg.username, "password": self._cfg.password, "grant_type": "password"}
        data = http_request_json("POST", url, timeout=self._cfg.request_timeout_seconds, data=payload)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError(
                f"Respuesta de login sin 'access_token' (claves recibidas: "
                f"{list(data.keys()) if isinstance(data, dict) else type(data)}; "
                "correr diagnose_iol_api.py para inspeccionar la respuesta cruda)."
            )
        self._token = token
        self._token_obtained_at = time.time()
        return True

    def _titulos_url(self, simbolo: str, action: str) -> str:
        version = f"{self._cfg.api_version_segment}/" if self._cfg.api_version_segment else ""
        return f"{self._cfg.base_url.rstrip('/')}/api/{version}{self._cfg.market}/Titulos/{simbolo}/{action}"

    def _authed_get(self, url: str):
        if not self._login():
            raise RuntimeError("no autenticado")
        headers = {"Authorization": f"Bearer {self._token}"}
        return http_get_json(url, timeout=self._cfg.request_timeout_seconds, headers=headers)

    @staticmethod
    def _parse_quote_record(symbol: str, rec: Dict) -> RawQuote:
        """
        Esquema CONFIRMADO contra una cuenta real (ver docstring de la
        clase): `ultimoPrecio` a nivel raiz, puntas de bid/ask en `puntas`
        (lista de niveles de profundidad; se toma el mejor nivel, `puntas[0]`).
        `puntas` puede venir vacia (`[]`) o `null` cuando no hay punta
        vigente (fuera de rueda, o una opcion sin operaciones recientes) -
        en ese caso se devuelve bid=ask=0 (sin punta), NO se fabrica un
        valor: el resto del pipeline (option_chain/filtros de liquidez) ya
        sabe descartar puntas en cero, el mismo criterio que con cualquier
        otra fuente cuando un simbolo no tiene mercado vigente.
        """
        if not isinstance(rec, dict):
            return RawQuote(symbol=symbol, bid=0.0, ask=0.0, bid_size=0.0, ask_size=0.0)
        puntas = rec.get("puntas")
        if isinstance(puntas, list) and puntas:
            level = puntas[0]
        elif isinstance(puntas, dict):
            level = puntas
        else:
            level = {}  # 'puntas' vacia/null: sin punta vigente, no fabricar un valor de otro campo
        return RawQuote(
            symbol=symbol,
            bid=_to_float(level.get("precioCompra")),
            ask=_to_float(level.get("precioVenta")),
            bid_size=_to_float(level.get("cantidadCompra")),
            ask_size=_to_float(level.get("cantidadVenta")),
            last_price=_to_float(rec.get("ultimoPrecio")),
            last_volume=_to_float(rec.get("montoOperado") or rec.get("volumenNominal")),
        )

    @staticmethod
    def _parse_option_record(rec: Dict) -> Optional[Tuple[str, OptionType, float, date]]:
        """
        Esquema CONFIRMADO contra una cuenta real (ver docstring de la
        clase): `simbolo`, `tipoOpcion` ("Call"/"Put", semantico y directo) y
        `fechaVencimiento` (ISO, semantico y directo) vienen en el propio
        registro - se usan como fuente PRIMARIA. El strike no viene como
        campo separado, asi que se extrae del simbolo (convencion de
        EXCHANGE - BYMA -, no de IOL en particular); ese mismo parseo de
        simbolo tambien sirve de FALLBACK para tipo/vencimiento si
        `tipoOpcion`/`fechaVencimiento` llegaran a faltar o no reconocerse.
        """
        if not isinstance(rec, dict):
            return None
        symbol = str(rec.get("simbolo") or "")
        if not symbol:
            return None

        cfg_i = SETTINGS.instruments
        option_type: Optional[OptionType] = None
        tipo_raw = str(rec.get("tipoOpcion") or "").strip().lower()
        if tipo_raw == "call":
            option_type = OptionType.CALL
        elif tipo_raw == "put":
            option_type = OptionType.PUT

        expiry: Optional[date] = None
        fecha_raw = rec.get("fechaVencimiento")
        if isinstance(fecha_raw, str) and fecha_raw:
            try:
                parsed_dt = datetime.fromisoformat(fecha_raw)
                if parsed_dt.year >= 2000:  # descarta fechas centinela tipo "0001-01-01"
                    expiry = parsed_dt.date()
            except ValueError:
                expiry = None

        parsed_from_symbol = _parse_data912_option_symbol(symbol, cfg_i.call_prefix, cfg_i.put_prefix)
        strike: Optional[float] = None
        if parsed_from_symbol is not None:
            symbol_type, symbol_strike, symbol_expiry = parsed_from_symbol
            strike = symbol_strike
            if option_type is None:
                option_type = symbol_type
            if expiry is None:
                expiry = symbol_expiry

        if option_type is None or strike is None or expiry is None:
            return None
        return symbol, option_type, strike, expiry

    def bootstrap(self) -> List[Tuple[str, OptionType, float, date]]:
        cfg_i = SETTINGS.instruments
        try:
            options = self._authed_get(self._titulos_url(cfg_i.underlying_symbol, "Opciones"))
        except Exception as exc:
            logger.warning("BrokerRestSource.bootstrap(): fallo al listar opciones (%s).", exc)
            return []

        candidates: List[Tuple[str, OptionType, float, date]] = []
        for rec in options or []:
            parsed = self._parse_option_record(rec)
            if parsed is None:
                logger.debug(
                    "BrokerRestSource.bootstrap(): registro descartado (simbolo=%r).",
                    rec.get("simbolo") if isinstance(rec, dict) else rec,
                )
                continue
            symbol, option_type, strike, expiry = parsed
            candidates.append((symbol, option_type, strike, expiry))
            # El propio listado ya trae la cotizacion embebida (rec['cotizacion'])
            # - se aprovecha para precargar el cache desde el arranque, sin
            # esperar al primer poll() para tener algo con que trabajar.
            quote_rec = rec.get("cotizacion") if isinstance(rec, dict) else None
            if quote_rec:
                self._quote_cache[symbol] = self._parse_quote_record(symbol, quote_rec)

        self._universe = candidates
        logger.info(
            "BrokerRestSource.bootstrap(): %d opciones de GGAL identificadas (de %d registros recibidos).",
            len(candidates), len(options or []),
        )
        return candidates

    def _refresh_near_the_money_quotes(self) -> None:
        """
        Refresca via GET INDIVIDUAL (no el batch de arriba) las puntas de
        las opciones dentro de una banda de moneyness alrededor del ultimo
        spot conocido.

        HALLAZGO REAL (2026-09-01, corriendo diagnose_iol_puntas.py contra
        una cuenta real en horario de rueda - ver seguimiento de la
        auditoria): el endpoint de CADENA (`/Titulos/GGAL/Opciones`)
        devuelve 'puntas': null para el 100% de los ~174 registros
        SIEMPRE, incluso para una opcion con una operacion reciente
        (ultimoPrecio=0.45, "GFGV4200SE") - o sea, no es que las opciones
        sean ilíquidas, ese endpoint especifico simplemente no trae
        profundidad de mercado. El endpoint INDIVIDUAL por simbolo (el
        mismo `_titulos_url(simbolo, "Cotizacion")` que ya se usa arriba
        para el SUBYACENTE) SI trae 'puntas' pobladas para el MISMO
        simbolo en el MISMO instante - confirmado contra la cuenta real.

        Sin este metodo, `valid_quotes` en run_bot.py siempre queda vacio
        (ver EntryScanDiagnostics/"Disponibilidad de cotizaciones") y la
        estrategia jamas puede evaluar ninguna señal, sin importar los
        umbrales configurados.

        Pedir las ~104 opciones individualmente en CADA poll (cada ~2s, ver
        run_bot.recompute_cycle) no es viable: arriesga empeorar los
        timeouts/503 que ya se observan contra la API de IOL. Por eso:
            - Se restringe a una banda de moneyness alrededor del spot
              (BrokerRestConfig.individual_quote_moneyness_band_pct) - las
              UNICAS opciones que la estrategia puede llegar a usar de
              verdad (entradas: LongFirstConfig.moneyness_band_pct=0.15;
              wings de spread: un poco mas alla del strike largo - de ahi
              el margen extra hasta 0.20).
            - Tope duro de simbolos por refresh (individual_quote_max_symbols)
              como valvula de seguridad adicional.
            - Throttle PROPIO, mas lento que el poll principal
              (individual_quote_min_refresh_interval_seconds) - las puntas
              de opciones no necesitan ser mas frescas que esto para una
              estrategia semanal.
            - En PARALELO, con un pool de threads DEDICADO (no el pool
              compartido de http_utils, dimensionado para 1-2 llamadas por
              poll) para no bloquear el ciclo entero esperando cada simbolo
              en secuencia.
        Si un simbolo puntual falla (timeout/error), se conserva la ultima
        punta conocida de ese simbolo en _quote_cache (mismo criterio
        "freeze, no starve" que el resto de la fuente) - una falla
        individual no aborta el refresh de los demas.
        """
        now = time.time()
        if (now - self._last_individual_quote_refresh_at) < self._cfg.individual_quote_min_refresh_interval_seconds:
            return
        if not self._universe:
            return

        cfg_i = SETTINGS.instruments
        spot_quote = self._quote_cache.get(cfg_i.underlying_symbol)
        if spot_quote is None:
            return
        reference_spot = spot_quote.last_price if spot_quote.last_price and spot_quote.last_price > 0 else None
        if reference_spot is None and spot_quote.bid > 0 and spot_quote.ask > 0:
            reference_spot = (spot_quote.bid + spot_quote.ask) / 2.0
        if reference_spot is None or reference_spot <= 0:
            return

        band = self._cfg.individual_quote_moneyness_band_pct
        scored: List[Tuple[float, str]] = []
        for symbol, _option_type, strike, _expiry in self._universe:
            if strike is None or strike <= 0:
                continue
            log_moneyness = abs(math.log(strike / reference_spot))
            if log_moneyness <= band:
                scored.append((log_moneyness, symbol))
        if not scored:
            return
        scored.sort(key=lambda item: item[0])
        symbols_to_refresh = [symbol for _lm, symbol in scored[: self._cfg.individual_quote_max_symbols]]

        if not self._login():
            return
        self._last_individual_quote_refresh_at = now
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = self._cfg.individual_quote_timeout_seconds

        def _fetch_one(symbol: str):
            return http_get_json(self._titulos_url(symbol, "Cotizacion"), timeout=timeout, headers=headers)

        futures = {
            _INDIVIDUAL_QUOTE_EXECUTOR.submit(_fetch_one, symbol): symbol
            for symbol in symbols_to_refresh
        }
        ok_count = 0
        try:
            for future in concurrent.futures.as_completed(futures, timeout=timeout + 10.0):
                symbol = futures[future]
                try:
                    data = future.result()
                    self._quote_cache[symbol] = self._parse_quote_record(symbol, data)
                    ok_count += 1
                except Exception as exc:
                    logger.debug(
                        "BrokerRestSource: fallo la punta individual de %s (%s); se mantiene el ultimo "
                        "dato conocido de ese simbolo.", symbol, exc,
                    )
        except concurrent.futures.TimeoutError:
            logger.warning(
                "BrokerRestSource: timeout global (%.0fs) refrescando puntas individuales - %d/%d "
                "completadas antes del corte; el resto sigue en curso en background y se descarta.",
                timeout + 10.0, ok_count, len(symbols_to_refresh),
            )

        logger.info(
            "BrokerRestSource: puntas individuales refrescadas para %d/%d opciones cercanas al spot "
            "(banda |log(K/S)|<=%.2f, spot ref=%.2f).",
            ok_count, len(symbols_to_refresh), band, reference_spot,
        )

    def fetch_snapshot(self) -> Tuple[Optional[RawQuote], Dict[str, RawQuote]]:
        cfg_i = SETTINGS.instruments
        # Se resetea en cada llamada: had_last_fetch_error() debe reflejar
        # SOLO este poll, no un fallo viejo ya recuperado.
        self._last_fetch_had_error = False
        try:
            data = self._authed_get(self._titulos_url(cfg_i.underlying_symbol, "Cotizacion"))
            self._quote_cache[cfg_i.underlying_symbol] = self._parse_quote_record(cfg_i.underlying_symbol, data)
        except Exception as exc:
            logger.warning("BrokerRestSource.fetch_snapshot(): fallo la cotizacion del subyacente (%s).", exc)
            self._last_fetch_had_error = True

        # La cadena ENTERA de opciones se refresca en UN SOLO request (el
        # mismo endpoint de bootstrap trae la cotizacion embebida por
        # opcion) - PERO (CORRECCION 2026-09-01, ver
        # _refresh_near_the_money_quotes() mas abajo): se confirmo contra
        # una cuenta real en horario de rueda que este endpoint de CADENA
        # nunca trae 'puntas' pobladas (siempre null, para el 100% de los
        # registros, incluso para una opcion con operaciones recientes) -
        # solo sirve para ultimoPrecio/volumenNominal/descubrir el universo
        # de simbolos, NO para el book. Se mantiene igual (es barato: 1
        # solo request) y se complementa abajo con puntas individuales
        # reales para las opciones cercanas al spot.
        try:
            options = self._authed_get(self._titulos_url(cfg_i.underlying_symbol, "Opciones"))
            for rec in options or []:
                if not isinstance(rec, dict):
                    continue
                symbol = str(rec.get("simbolo") or "")
                quote_rec = rec.get("cotizacion")
                if symbol and quote_rec:
                    self._quote_cache[symbol] = self._parse_quote_record(symbol, quote_rec)
        except Exception as exc:
            logger.warning("BrokerRestSource.fetch_snapshot(): fallo al refrescar la cadena de opciones (%s).", exc)
            self._last_fetch_had_error = True

        # Puntas REALES por opcion (ver _refresh_near_the_money_quotes()):
        # complementa (sobreescribe en _quote_cache) las entradas de arriba
        # para las opciones cercanas al spot, unicas que la estrategia
        # puede llegar a usar. Con su propio throttle (mas lento que este
        # poll) y su propio pool de threads - una falla aca NO cuenta como
        # error de este fetch_snapshot() (no es la fuente primaria de spot/
        # cadena, es un complemento best-effort).
        self._refresh_near_the_money_quotes()

        # NOTA (BUG REAL CORREGIDO, ver had_last_fetch_error() y el
        # seguimiento de la auditoria del 2026-09-01): spot_quote/
        # option_quotes se siguen devolviendo desde _quote_cache aunque haya
        # habido un error arriba - a proposito, es el comportamiento de
        # "freeze, no starve" ya establecido (las puntas conocidas siguen
        # disponibles para salidas/hedge). Lo que cambia es que ahora
        # had_last_fetch_error() le permite a LiveShadowFeed.poll() SABER
        # que este resultado viene de cache y no de un refresh exitoso, para
        # que el contador de failover no quede ciego a fallos parciales
        # sostenidos.
        spot_quote = self._quote_cache.get(cfg_i.underlying_symbol)
        option_quotes = {s: q for s, q in self._quote_cache.items() if s != cfg_i.underlying_symbol}
        return spot_quote, option_quotes


# Factoria de fuentes por nombre, usada por LiveShadowFeed para resolver
# ShadowConfig.source_priority() (ej. "primary_ws,data912,mock") en
# instancias concretas. Agregar aca cualquier fuente nueva que se sume en el
# futuro para que quede disponible en la lista de prioridad configurable.
_SOURCE_FACTORIES: Dict[str, Callable[[], "ShadowDataSource"]] = {
    "primary_ws": PrimaryMarketDataSource,
    "data912": Data912RestSource,
    "broker_rest": BrokerRestSource,
    "mock": MockReplaySource,
}


# ---------------------------------------------------------------------------
# LiveShadowFeed: adapter de alto nivel, misma interfaz que MarketDataFeed
# ---------------------------------------------------------------------------

class LiveShadowFeed:
    """
    Reemplazo de MarketDataFeed para el modo Shadow Trading: en vez de
    suscribirse (solamente) a un websocket de PyRofex, puede correr contra
    cualquiera de las fuentes en `_SOURCE_FACTORIES` (Primary/pyRofex via
    PrimaryMarketDataSource, REST publico via Data912RestSource, un scaffold
    de broker local via BrokerRestSource, o el generador local
    MockReplaySource), en el orden de preferencia que define
    `SETTINGS.shadow.source_priority()`, con failover automatico si la
    fuente activa deja de responder de forma sostenida. Traduce cada punta a
    un OrderBookSnapshot exactamente igual que MarketDataFeed.handle_market_data.
    Ver run_bot.py para el cableado condicional (SETTINGS.shadow.enabled).

    Resiliencia multi-fuente (ver config.ShadowConfig): la fuente activa se
    resuelve al construir la instancia (`_instantiate_first_available`,
    probando en orden hasta la primera que responda `is_available()==True`)
    y se reevalua en cada `poll()`:
        - Tras `source_failure_threshold` polls CONSECUTIVOS sin ningun dato
          util (spot None y ninguna opcion), se conmuta a la siguiente
          fuente disponible en la prioridad (`_advance_to_next_source`) y se
          re-descubre el universo de instrumentos contra ella (los simbolos
          de una fuente no son validos para otra: "GFGC4200AG" en data912
          no significa nada para pyRofex, que usaria "MERV - XMEV -
          GFGC4200AG - 24hs" o similar).
        - Si TODAS las fuentes configuradas fallan, se cae incondicionalmente
          a MockReplaySource (100 por ciento local, nunca falla) como red de
          seguridad final - el bot jamas se queda sin ninguna fuente de datos.
        - Periodicamente (`source_reprobe_interval_seconds`) se reintenta
          volver a una fuente de MAYOR prioridad que la activa, si ya hubo
          un failover previo (`_maybe_reprobe_higher_priority`) - sin esto,
          un failover a "mock" por una caida transitoria de la fuente
          preferida seria permanente para el resto de la corrida.
    En ningun caso una falla de fuente interrumpe la estrategia ni tira el
    proceso: todas las llamadas a metodos de fuente pasan por `_safe_call`,
    que atrapa cualquier excepcion y degrada a un valor por defecto.
    """

    def __init__(self, on_book_update):
        self.on_book_update = on_book_update
        self._known_symbols: set = set()
        self._option_chain: Optional[OptionChain] = None
        self._priority: Tuple[str, ...] = SETTINGS.shadow.source_priority()
        self._source_index = 0
        self._source: ShadowDataSource = self._instantiate_first_available()
        self._consecutive_failures = 0
        self._last_reprobe_at: Optional[float] = None

    # -- Seleccion de fuente / failover -------------------------------------

    @staticmethod
    def _safe_call(fn: Callable[[], object], default: object, label: str = "") -> object:
        """
        Ejecuta `fn` (una llamada sin argumentos, tipicamente un metodo
        bound de la fuente activa) atrapando CUALQUIER excepcion: ninguna
        fuente (ni siquiera una mal implementada por el usuario, ver
        BrokerRestSource) debe poder tirar el proceso principal. Devuelve
        `default` si `fn` lanza.
        """
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - ver docstring: nunca debe propagar
            logger.warning("Shadow feed: %s lanzo una excepcion (%s); se ignora.", label or fn, exc)
            return default

    def _probe(self, name: str) -> Optional[ShadowDataSource]:
        """Instancia la fuente `name` y devuelve la instancia si is_available()==True, si no None."""
        factory = _SOURCE_FACTORIES.get(name)
        if factory is None:
            logger.warning("Shadow feed: nombre de fuente desconocido '%s' en source_priority; se ignora.", name)
            return None
        source = factory()
        available = self._safe_call(source.is_available, default=False, label=f"{name}.is_available()")
        return source if available else None

    def _instantiate_first_available(self) -> ShadowDataSource:
        """
        Recorre SETTINGS.shadow.source_priority() en orden y devuelve la
        primera fuente cuyo is_available() de True. Si ninguna responde
        (caso extremo: sin red saliente Y pyRofex no instalado/sin
        credenciales), cae a MockReplaySource incondicionalmente - la unica
        fuente 100 por ciento local, que nunca falla.
        """
        for i, name in enumerate(self._priority):
            source = self._probe(name)
            if source is not None:
                logger.info("Shadow feed: fuente activa = '%s' (prioridad %d/%d).", name, i + 1, len(self._priority))
                self._source_index = i
                return source
            logger.warning("Shadow feed: fuente '%s' no disponible, se prueba la siguiente en la prioridad.", name)

        logger.warning(
            "Shadow feed: ninguna fuente de la prioridad %s respondio; se usa Mock/Replay incondicionalmente "
            "como ultimo recurso (100 por ciento local, nunca falla).", self._priority,
        )
        self._source_index = len(self._priority)
        return MockReplaySource()

    def _advance_to_next_source(self) -> bool:
        """
        Avanza a la siguiente fuente disponible en la prioridad configurada
        (o a Mock/Replay si ya se agoto la lista). Devuelve False si no hay
        a donde avanzar (ya se esta en Mock/Replay - no hay nada mas
        conservador a donde caer). Reinicia el conteo de fallas
        consecutivas: cada fuente nueva empieza su propio conteo desde cero.
        """
        if isinstance(self._source, MockReplaySource):
            return False

        for i in range(self._source_index + 1, len(self._priority)):
            name = self._priority[i]
            source = self._probe(name)
            if source is not None:
                logger.warning(
                    "Shadow feed: failover (fallaron %d polls consecutivos) -> fuente '%s' (prioridad %d/%d).",
                    self._consecutive_failures, name, i + 1, len(self._priority),
                )
                self._source = source
                self._source_index = i
                self._consecutive_failures = 0
                return True

        logger.warning(
            "Shadow feed: se agotaron todas las fuentes configuradas en source_priority (%s); "
            "failover final a Mock/Replay (100 por ciento local, nunca falla).", self._priority,
        )
        self._source = MockReplaySource()
        self._source_index = len(self._priority)
        self._consecutive_failures = 0
        return True

    def _maybe_reprobe_higher_priority(self, now: float) -> None:
        """
        Si ya hubo un failover (self._source_index > 0, es decir no se esta
        en la fuente mas preferida), reintenta periodicamente volver a
        alguna fuente de MAYOR prioridad que la activa. Deliberadamente NO
        instantaneo (ver ShadowConfig.source_reprobe_interval_seconds):
        reintentar en cada poll podria generar "flapping" si la fuente
        preferida esta intermitente en vez de caida del todo.
        """
        if self._source_index <= 0:
            return
        interval = SETTINGS.shadow.source_reprobe_interval_seconds
        if self._last_reprobe_at is not None and (now - self._last_reprobe_at) < interval:
            return
        self._last_reprobe_at = now

        for i in range(0, min(self._source_index, len(self._priority))):
            name = self._priority[i]
            source = self._probe(name)
            if source is not None:
                logger.info(
                    "Shadow feed: '%s' (mayor prioridad que la fuente activa) volvio a estar disponible; "
                    "se vuelve a esa fuente.", name,
                )
                self._source = source
                self._source_index = i
                self._consecutive_failures = 0
                self._bootstrap_from_current_source()
                return

    # -- Bootstrap del universo de instrumentos -----------------------------

    def bootstrap_universe(self, option_chain: OptionChain) -> List[str]:
        """
        Equivalente shadow de MarketDataFeed.bootstrap_universe(): puebla
        `option_chain` con una OptionQuote por base (sin datos de mercado
        todavia; llegan en el primer poll()) y devuelve la lista de
        "tickers" (simbolos) a considerar suscriptos. Guarda `option_chain`
        para poder re-bootstrapear automaticamente si mas adelante ocurre un
        failover dentro de poll() (ver _bootstrap_from_current_source).
        """
        self._option_chain = option_chain
        return self._bootstrap_from_current_source()

    def _bootstrap_from_current_source(self) -> List[str]:
        """
        Descubre el universo contra self._source; si no devuelve nada
        utilizable, sigue probando el resto de la prioridad (failover
        tambien durante el bootstrap, no solo en poll()) antes de resignarse
        a Mock/Replay. Al terminar, tambien dispara la suscripcion real de
        la fuente resultante (relevante para PrimaryMarketDataSource; no-op
        para las fuentes basadas en polling) para que un failover ocurrido
        DESPUES del bootstrap inicial (dentro de poll()) deje la fuente
        nueva realmente recibiendo datos sin depender de que run_bot.py
        vuelva a llamar a subscribe() por su cuenta.
        """
        option_chain = self._option_chain
        candidates = self._safe_call(self._source.bootstrap, default=[], label=f"{type(self._source).__name__}.bootstrap()")

        while not candidates and not isinstance(self._source, MockReplaySource):
            if not self._advance_to_next_source():
                break
            candidates = self._safe_call(
                self._source.bootstrap, default=[], label=f"{type(self._source).__name__}.bootstrap()",
            )

        cfg = SETTINGS.instruments
        today = date.today()
        distinct_expiries = sorted({c[3] for c in candidates if c[3] >= today})
        kept_expiries = set(distinct_expiries[: cfg.expiries_ahead])

        tickers: List[str] = [cfg.contado_ticker]
        known_symbols: set = set()
        count = 0
        for symbol, option_type, strike, expiry in candidates:
            if expiry not in kept_expiries:
                continue
            days_cal = (expiry - today).days
            days_biz = _business_days_between(today, expiry)
            placeholder_book = OrderBookSnapshot(symbol=symbol, bid=0.0, ask=0.0, bid_size=0.0, ask_size=0.0)
            quote = OptionQuote(
                symbol=symbol, strike=strike, expiry=expiry, option_type=option_type,
                book=placeholder_book, days_calendar=days_cal, days_business=days_biz,
            )
            if option_chain is not None:
                option_chain.upsert_quote(quote)
            tickers.append(symbol)
            known_symbols.add(symbol)
            count += 1

        self._known_symbols = known_symbols
        self._safe_call(lambda: self._source.subscribe(tickers), default=None,
                         label=f"{type(self._source).__name__}.subscribe()")
        logger.info(
            "Shadow feed: universo listo con %d opciones en %d vencimiento(s) (%s), fuente=%s.",
            count, len(kept_expiries),
            ", ".join(e.isoformat() for e in sorted(kept_expiries)),
            type(self._source).__name__,
        )
        return tickers

    def subscribe(self, tickers: List[str]) -> None:
        """
        Llamado una vez por run_bot.py tras bootstrap_universe(). Para las
        fuentes basadas en polling (Data912RestSource, MockReplaySource,
        BrokerRestSource) esto es puramente informativo: poll() ya cumple el
        rol de "suscripcion" en cada ciclo. Para PrimaryMarketDataSource,
        delega en su subscribe() real (que ya fue invocado una vez desde
        _bootstrap_from_current_source(); esta segunda llamada es idempotente,
        ver PrimaryMarketDataSource.subscribe()).
        """
        self._safe_call(lambda: self._source.subscribe(tickers), default=None,
                         label=f"{type(self._source).__name__}.subscribe()")
        logger.info("Shadow feed: %d instrumentos bajo seguimiento (fuente=%s).",
                    len(tickers), type(self._source).__name__)

    def poll(self, option_chain: OptionChain) -> None:
        """
        Llamar una vez por ciclo (ver run_bot.py, rama shadow del loop
        principal) en lugar de esperar callbacks de websocket. Traduce el
        snapshot actual de la fuente activa a OrderBookSnapshot y lo
        despacha via el mismo callback `on_book_update` que usa
        MarketDataFeed, para que el resto del pipeline
        (option_chain.recompute_all -> IV/griegas -> señales) no tenga que
        distinguir el origen del dato. Tambien administra el failover
        automatico (ver docstring de la clase): nunca lanza ni deja el
        pipeline sin datos por una fuente que fallo.
        """
        self._maybe_reprobe_higher_priority(time.time())

        spot_quote, option_quotes = self._safe_call(
            self._source.fetch_snapshot, default=(None, {}), label=f"{type(self._source).__name__}.fetch_snapshot()",
        )
        # BUG REAL CORREGIDO (ver ShadowDataSource.had_last_fetch_error() y
        # BrokerRestSource.fetch_snapshot(): seguimiento de la auditoria del
        # 2026-09-01 en produccion): antes solo "spot_quote is None and not
        # option_quotes" contaba como fallo para el failover, pero
        # BrokerRestSource sigue devolviendo el ultimo dato cacheado cuando
        # el refresh en vivo falla, asi que ese chequeo casi nunca detectaba
        # un problema real (se vio en produccion: >1 hora de fallos
        # sostenidos de refresh de la cadena de opciones sin que el failover
        # se disparara ni una vez). had_last_fetch_error() cierra ese hueco
        # sin cambiar el comportamiento de las fuentes que no lo necesitan
        # (default False en ShadowDataSource).
        had_soft_error = self._safe_call(
            self._source.had_last_fetch_error, default=False,
            label=f"{type(self._source).__name__}.had_last_fetch_error()",
        )

        if (spot_quote is None and not option_quotes) or had_soft_error:
            self._consecutive_failures += 1
            if self._consecutive_failures >= SETTINGS.shadow.source_failure_threshold:
                if self._advance_to_next_source():
                    # Failover: el universo de simbolos es propio de cada
                    # fuente (convenciones de nombre distintas), asi que hay
                    # que re-descubrirlo contra la fuente nueva antes de que
                    # el poll tenga sentido. Se reintenta el fetch en el
                    # mismo ciclo para no perder un poll entero de datos
                    # frescos tras el failover.
                    self._bootstrap_from_current_source()
                    spot_quote, option_quotes = self._safe_call(
                        self._source.fetch_snapshot, default=(None, {}),
                        label=f"{type(self._source).__name__}.fetch_snapshot()",
                    )
        else:
            self._consecutive_failures = 0

        if spot_quote is not None:
            book = OrderBookSnapshot(
                symbol=SETTINGS.instruments.contado_ticker,
                bid=spot_quote.bid, ask=spot_quote.ask,
                bid_size=spot_quote.bid_size, ask_size=spot_quote.ask_size,
                last_volume=spot_quote.last_volume,
                # BUG REAL CORREGIDO: se propaga el `as_of` ORIGINAL del
                # RawQuote (cuando se parseo de verdad), no la hora de este
                # despacho - ver docstring de RawQuote.as_of / OrderBookSnapshot.as_of.
                # Sin esto, un spot cacheado y reproducido durante un corte
                # sostenido de BrokerRestSource se veia "fresco" en cada
                # poll para _spot_last_update_at (run_bot.py), dejando
                # ciega a RiskConfig.max_market_data_staleness_seconds.
                as_of=spot_quote.as_of,
            )
            self.on_book_update(SETTINGS.instruments.contado_ticker, book)

        for symbol in self._known_symbols:
            raw = option_quotes.get(symbol)
            if raw is None:
                continue
            book = OrderBookSnapshot(
                symbol=symbol, bid=raw.bid, ask=raw.ask,
                bid_size=raw.bid_size, ask_size=raw.ask_size, last_volume=raw.last_volume,
                as_of=raw.as_of,  # ver comentario equivalente arriba, mismo motivo
            )
            self.on_book_update(symbol, book)
