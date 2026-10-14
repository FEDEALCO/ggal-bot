"""
option_chain.py
================
Estructuras de mercado (matriz de puntas) y la cadena de opciones completa
de GGAL: por cada instrumento (contado y cada base call/put) se guarda el
ultimo snapshot de puntas, y por cada opcion se recalcula IV y griegas cada
vez que llega un update de precio. Ver docs de diseño, seccion 2.2.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from ggal_bot.models.black_scholes import BlackScholesGreeks, OptionType
from ggal_bot.models.implied_vol import ImpliedVolatilityCalculator


@dataclass
class OrderBookSnapshot:
    """
    Foto de una punta de mercado en un instante dado.

    `as_of` (BUG REAL CORREGIDO, ver docs/AUDITORIA_MAESTRA_2026-08-27.md,
    seguimiento del 2026-08-31): timestamp Unix (epoch, `time.time()`) de
    cuando esta punta fue REALMENTE observada en el mercado - no cuando fue
    despachada a `on_book_update`. La diferencia importa en modo Shadow: si
    la fuente activa (ver BrokerRestSource.fetch_snapshot()) no logra
    refrescar un simbolo en un poll puntual, reproduce la ULTIMA cotizacion
    buena que tiene cacheada para no dejar el pipeline sin dato - pero sin
    este campo, esa cotizacion vieja se despachaba como si fuera nueva en
    CADA poll, dejando cualquier guardia de staleness basada en "hubo un
    update recientemente" completamente ciega (siempre veia updates
    recientes, aunque el VALOR fuera de hace una hora). Con `as_of`
    preservado desde el origen (ver data/live_shadow_feed.py:RawQuote.as_of
    y LiveShadowFeed.poll()), tanto la guardia de spot
    (RiskConfig.max_market_data_staleness_seconds) como la guardia por
    opcion (RiskConfig.max_option_quote_staleness_seconds, `is_stale()` de
    abajo) miden la antiguedad REAL del dato, no la cadencia de polling.
    Default `time.time()`: para fuentes que SIEMPRE entregan datos frescos
    en el momento en que se construye el snapshot (websocket real de
    pyRofex, Data912RestSource, MockReplaySource), no hace falta pasarlo
    explicitamente.
    """
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    last_volume: float = 0.0
    # last_price (MEJORA 2026-10-13, Fase 0/Hipotesis 1 del mandato de
    # research cuantitativo): ultimo precio REALMENTE operado en el
    # mercado (distinto de `mid`, que es un punto medio de puntas, nunca
    # un trade real). Ya se parseaba desde la fuente real (data912: campo
    # "c"; pyRofex: "LA"."price") en data.live_shadow_feed.RawQuote/
    # data.market_data_feed, pero se descartaba al construir este objeto -
    # ver MarketSnapshotLogger, que ahora lo persiste junto con
    # last_volume para poder calibrar un modelo de fill pasivo (Hipotesis
    # 1: provision de liquidez) sin inventar ningun dato.
    last_price: float = 0.0
    as_of: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_relative(self) -> float:
        if self.mid <= 0:
            return math.inf
        return self.spread / self.mid

    def age_seconds(self, now: Optional[float] = None) -> float:
        """Segundos transcurridos desde `as_of` hasta `now` (default: `time.time()`)."""
        return (now if now is not None else time.time()) - self.as_of

    def is_stale(self, max_age_seconds: float, now: Optional[float] = None) -> bool:
        """True si esta punta ya supera `max_age_seconds` de antiguedad real (ver docstring de la clase)."""
        return self.age_seconds(now) > max_age_seconds

    def is_tradeable(self, max_spread_relative: float, min_size: float) -> bool:
        if self.bid <= 0 or self.ask <= 0:
            return False
        if self.spread_relative > max_spread_relative:
            return False
        if min(self.bid_size, self.ask_size) < min_size:
            return False
        return True

    def is_valid_quote(
        self, max_spread_relative: float, max_age_seconds: float, now: Optional[float] = None,
    ) -> bool:
        """
        Filtro unificado de "quote operable" (dos puntas + spread maximo +
        antiguedad maxima) - MEJORA 2026-10-13 (Fase 0 del mandato de
        research cuantitativo, a pedido explicito del usuario: "Construi el
        filtro de quote operable (dos puntas, spread maximo, antiguedad
        maxima) y aplicalo a stops y salidas: si el quote no pasa, el stop
        espera al primer quote valido"). Pensado para decidir si un book es
        lo bastante bueno para ACTUAR sobre su precio marcando una posicion
        YA ABIERTA (ej. medir PnL% de Stop Loss/Take Profit) - ver
        RiskConfig.enforce_quote_validity_filter_for_exits y su uso en
        run_bot.py al armar `current_prices`.

        A diferencia de `is_tradeable()` (pensado para decidir si se puede
        EJECUTAR una orden nueva de cierto tamaño), este metodo
        deliberadamente NO exige un tamaño minimo de punta (`min_size`): el
        tamaño de la punta es un criterio de ejecutabilidad de una orden
        nueva, no de si el PRECIO que esa punta implica es confiable para
        marcar a mercado una posicion que ya existe. Reutiliza exactamente
        la misma nocion de "stale" que `is_stale()` (abajo) - mismo campo
        `as_of`, ninguna logica nueva de antiguedad.
        """
        if self.bid <= 0 or self.ask <= 0:
            return False
        if self.spread_relative > max_spread_relative:
            return False
        if self.is_stale(max_age_seconds, now):
            return False
        return True


@dataclass
class OptionQuote:
    symbol: str
    strike: float
    expiry: date
    option_type: OptionType
    book: OrderBookSnapshot
    days_calendar: int
    days_business: int
    spot_ref: float = 0.0                 # spot de GGAL usado en el ultimo calculo de IV
    iv: Optional[float] = None
    greeks: Optional[Dict[str, float]] = None

    def compute_iv_and_greeks(
        self,
        spot: float,
        rate: float,
        iv_calc: ImpliedVolatilityCalculator,
        dividend_yield: float = 0.0,
        sigma_guess: float = 0.35,
    ) -> None:
        bs = BlackScholesGreeks(
            spot=spot,
            strike=self.strike,
            rate=rate,
            dividend_yield=dividend_yield,
            days_calendar=self.days_calendar,
            days_business=self.days_business,
            option_type=self.option_type,
        )
        sigma = iv_calc.solve(bs, self.book.mid, sigma_guess=sigma_guess)
        self.spot_ref = spot
        self.iv = sigma
        self.greeks = bs.all_greeks(sigma) if sigma is not None else None


class OptionChain:
    """
    Contenedor de todas las OptionQuote vigentes para GGAL, agrupadas por
    vencimiento. Se actualiza en cada tick de mercado (ver data/market_data_feed.py)
    y expone metodos para recalcular toda la cadena de una vez.
    """

    def __init__(self):
        self._quotes: Dict[str, OptionQuote] = {}

    def upsert_quote(self, quote: OptionQuote) -> None:
        self._quotes[quote.symbol] = quote

    def update_book(self, symbol: str, book: OrderBookSnapshot) -> None:
        if symbol in self._quotes:
            self._quotes[symbol].book = book

    def get(self, symbol: str) -> Optional[OptionQuote]:
        return self._quotes.get(symbol)

    def all_quotes(self) -> List[OptionQuote]:
        return list(self._quotes.values())

    def quotes_by_expiry(self) -> Dict[date, List[OptionQuote]]:
        out: Dict[date, List[OptionQuote]] = {}
        for q in self._quotes.values():
            out.setdefault(q.expiry, []).append(q)
        return out

    def recompute_all(
        self,
        spot: float,
        rate: float,
        iv_calc: ImpliedVolatilityCalculator,
        dividend_yield: float = 0.0,
        sigma_guess: float = 0.35,
        max_quote_age_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> Optional[int]:
        """
        Recalcula IV/griegas de toda la cadena contra el `spot` actual.

        `max_quote_age_seconds` (BUG REAL CORREGIDO, ver docstring de
        OrderBookSnapshot y RiskConfig.max_option_quote_staleness_seconds):
        si se pasa, cualquier opcion cuyo `book` ya supere ese umbral de
        antiguedad NO se recalcula este ciclo - se deja el ultimo IV/griega
        conocido tal cual, en vez de mezclar un `spot` FRESCO con un precio
        de opcion VIEJO (eso produciria un IV internamente inconsistente,
        que puede leerse como una "dislocacion de smile" real sin serlo).
        `now` inyectable para tests, mismo patron que el resto del ciclo.
        Devuelve la cantidad de opciones salteadas por staleness (None si no
        se paso `max_quote_age_seconds`), para que el llamador pueda loguear
        una unica alerta agregada en vez de una por simbolo por ciclo.
        """
        stale_count: Optional[int] = None
        for q in self._quotes.values():
            if q.book.bid > 0 and q.book.ask > 0:
                if max_quote_age_seconds is not None and q.book.is_stale(max_quote_age_seconds, now=now):
                    stale_count = (stale_count or 0) + 1
                    continue
                q.compute_iv_and_greeks(spot, rate, iv_calc, dividend_yield, sigma_guess)
        return stale_count
