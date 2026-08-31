#!/usr/bin/env python3
"""
run_bot.py
==========
Punto de entrada del bot. Orquesta el flujo completo:

    Conexion (order_gateway.initialize_environment + WebSocketConnectionManager)
        -> Bootstrap Universe (market_data_feed.bootstrap_universe)
        -> Suscripcion de Market Data (market_data_feed.subscribe)
        -> Monitoreo de Griegas (IV/Griegas -> superficie de vol -> señales)
        -> Disparo de Coberturas (strategy.delta_hedger) y Arbitrajes
           (execution.mid_price_exec)
        -> Graceful Shutdown (cancela ordenes abiertas, cierra el websocket,
           persiste el estado final)

Uso:
    python run_bot.py            # corre contra REMARKET (paper trading) por defecto
    (ver .env / ggal_bot/config.py para apuntar a LIVE)

IMPORTANTE: este orquestador asume que PyRofex esta instalado y configurado
(ver .env.example). Sin esas credenciales, el motor cuantitativo puede
probarse igual con datos simulados corriendo
`python -m ggal_bot.validation.test_quant_engine`.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ggal_bot.config import SETTINGS, VALID_STRATEGIES
from ggal_bot.paths import LOG_FILE
from ggal_bot.data.market_data_feed import MarketDataFeed
from ggal_bot.data.live_shadow_feed import LiveShadowFeed
from ggal_bot.data.option_chain import OptionChain, OrderBookSnapshot
from ggal_bot.models.implied_vol import ImpliedVolatilityCalculator
from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.portfolio.portfolio import Portfolio, Position
from ggal_bot.risk.risk_manager import RiskLimits, RiskManager
from ggal_bot.risk.position_sizer import PositionSizer
from ggal_bot.execution.market_making import MarketMakingEngine
from ggal_bot.execution.mid_price_exec import MidPriceExecutionEngine
from ggal_bot.execution.order_gateway import (
    OrderGateway,
    OrderSide,
    OrderStatus,
    WebSocketConnectionManager,
    initialize_environment,
)
from ggal_bot.strategy.delta_hedger import DeltaHedgingEngine
from ggal_bot.strategy.vol_arbitrage import VolatilityArbitrageStrategy
from ggal_bot.strategy.weekly_asymmetric import WeeklyAsymmetricStrategy
from ggal_bot.data.technical_analysis import TechnicalAnalysisEngine, Trend
from ggal_bot.state_writer import StateWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
logger = logging.getLogger("ggal_bot.run_bot")


class GgalOptionsBot:
    def __init__(self, order_gateway: Optional[OrderGateway] = None):
        """
        `order_gateway`: override explicito (ver
        ggal_bot.execution.order_gateway.OrderGateway) - inyectable
        principalmente para tests, que necesitan un ShadowAuditLogger
        aislado en un path temporal en vez del CSV real de produccion (ver
        docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.3 y
        ggal_bot/validation/conftest.py). Si se omite, se construye uno
        nuevo con la configuracion por defecto (comportamiento identico al
        de antes de este parametro).
        """
        # -- Motor cuantitativo y estado de mercado --------------------------
        self.option_chain = OptionChain()
        self.portfolio = Portfolio()
        self.iv_calc = ImpliedVolatilityCalculator()

        # -- Riesgo y estrategia ----------------------------------------------
        self.risk_manager = RiskManager(RiskLimits(
            max_vega_total=SETTINGS.risk.max_vega_total,
            max_gamma_total=SETTINGS.risk.max_gamma_total,
            max_spread_relative=SETTINGS.risk.max_spread_relative,
            min_book_size=SETTINGS.risk.min_book_size,
            min_daily_volume=SETTINGS.risk.min_daily_volume,
        ))
        # Seleccion de estrategia activa (ver config.StrategyConfig /
        # GGAL_BOT_ACTIVE_STRATEGY): "weekly_asymmetric" (Long-First, DEFAULT)
        # o "vol_arbitrage" (arbitraje delta-neutral original). Un valor
        # invalido no frena el arranque - cae al default con una advertencia
        # explicita, en vez de fallar en silencio o crashear el proceso.
        active_strategy_name = SETTINGS.strategy.active
        if active_strategy_name not in VALID_STRATEGIES:
            logger.warning(
                "GGAL_BOT_ACTIVE_STRATEGY=%r no es valido (opciones: %s); se usa "
                "'weekly_asymmetric' por defecto.",
                active_strategy_name, ", ".join(VALID_STRATEGIES),
            )
            active_strategy_name = "weekly_asymmetric"
        self.active_strategy_name = active_strategy_name

        # `self.position_sizer` solo existe bajo weekly_asymmetric: el modo
        # vol_arbitrage sigue con el tamaño fijo de 1 contrato (ver TODO en
        # _act_on_signal) - dimensionar dinamicamente ese modo tambien queda
        # fuera del alcance de este cambio.
        self.position_sizer: Optional[PositionSizer] = None
        # Motor de Analisis Tecnico 1D (ver data/technical_analysis.py): solo
        # se instancia bajo weekly_asymmetric, que es el unico modo que
        # consume el filtro direccional obligado (BULLISH/BEARISH/NEUTRAL).
        # El modo vol_arbitrage original queda sin cambios de comportamiento.
        self.technical_engine: Optional[TechnicalAnalysisEngine] = None
        if self.active_strategy_name == "vol_arbitrage":
            self.strategy = VolatilityArbitrageStrategy(
                self.risk_manager, smile_threshold_vol_points=SETTINGS.signal.smile_threshold_vol_points,
            )
        else:  # "weekly_asymmetric" (default)
            self.strategy = WeeklyAsymmetricStrategy(self.risk_manager, config=SETTINGS.long_first)
            self.position_sizer = PositionSizer()
            self.technical_engine = TechnicalAnalysisEngine(config=SETTINGS.technical_analysis)
        # Ultimo TechnicalSnapshot ya logueado (por identidad de objeto, ver
        # _run_weekly_asymmetric_cycle) - evita repetir la misma linea de
        # "Tendencia 1D GGAL: ..." en cada ciclo mientras el cache del motor
        # tecnico siga vigente.
        self._last_ta_snapshot_logged = None
        logger.info("Estrategia activa: %s", self.active_strategy_name)

        self.delta_hedger = DeltaHedgingEngine(delta_band=SETTINGS.risk.delta_band)

        # -- Ejecucion -----------------------------------------------------
        self.mm_engine = MarketMakingEngine(
            tick_size=SETTINGS.execution.tick_size,
            liquid_spread_relative_threshold=SETTINGS.execution.liquid_spread_relative_threshold,
        )
        self.order_gateway = order_gateway if order_gateway is not None else OrderGateway()
        self.mid_price_exec = MidPriceExecutionEngine(self.order_gateway, self.mm_engine)

        # -- Persistencia de estado ------------------------------------------
        self.state_writer = StateWriter()

        # -- Conectividad de mercado ------------------------------------------
        # Shadow Trading (ver ggal_bot/data/live_shadow_feed.py): cuando esta
        # activo, el bot no abre ninguna conexion real de PyRofex - ni para
        # datos ni para ordenes (ver connect_and_subscribe() y
        # execution/order_gateway.py). Sirve para validar la logica
        # cuantitativa contra un ambiente sin la cadena de opciones de GGAL
        # aprovisionada (ver diagnose_instruments.py).
        self.shadow_mode = SETTINGS.shadow.enabled
        if self.shadow_mode:
            logger.warning(
                "SHADOW MODE activo (GGAL_BOT_SHADOW_MODE=true): el bot NO se "
                "conecta a PyRofex ni envia ordenes reales. Datos via "
                "data912.com o Mock/Replay; fills simulados en logs/shadow_trades.csv."
            )
            self.market_feed = LiveShadowFeed(on_book_update=self._on_book_update)
        else:
            self.market_feed = MarketDataFeed(on_book_update=self._on_book_update)
        self.ws_manager: Optional[WebSocketConnectionManager] = None
        self._subscribed_tickers: List[str] = []

        self._spot_book: Optional[OrderBookSnapshot] = None
        self._recent_volumes: Dict[str, float] = {}
        self._shutting_down = False

        # Guardia de staleness de datos de mercado (ver RiskConfig.
        # max_market_data_staleness_seconds / _is_market_data_stale()):
        # timestamp de la ultima vez que llego una punta del SPOT de GGAL
        # (contado o futuro), sea por poll() exitoso en modo Shadow o por
        # callback real de websocket. None hasta el primer dato (el arranque
        # ya esta cubierto por separado: recompute_cycle() no avanza
        # mientras self._spot_book siga en None).
        self._spot_last_update_at: Optional[datetime] = None
        # Dedup de logging (mismo patron que _last_ta_snapshot_logged): evita
        # repetir la alerta de staleness en cada ciclo de ~2-4s mientras dura
        # la caida, y loguea una unica vez tambien cuando se recupera.
        self._market_data_stale_logged = False
        # Idem, para la guardia de staleness POR OPCION (ver
        # RiskConfig.max_option_quote_staleness_seconds / recompute_cycle()):
        # evita repetir la alerta cada ciclo mientras haya opciones stale.
        self._option_staleness_logged = False

    # -- Callbacks de mercado ---------------------------------------------

    def _on_book_update(self, symbol: str, book: OrderBookSnapshot) -> None:
        """
        Callback liviano registrado en MarketDataFeed: solo actualiza el
        estado en memoria (spot o book de una opcion). El recalculo pesado
        (IV, griegas, superficie de vol) se hace en el loop principal
        (recompute_cycle), no aca, para no bloquear el hilo del websocket
        con computo intensivo en cada tick.
        """
        if symbol == SETTINGS.instruments.contado_ticker or symbol == SETTINGS.instruments.futuro_ticker:
            self._spot_book = book
            # Marca de tiempo para la guardia de staleness (ver
            # _is_market_data_stale()): se actualiza SOLO con el spot, no con
            # cada opcion individual - el spot es el dato mas critico (todo
            # el pipeline de IV/griegas/señales depende de el).
            #
            # BUG REAL CORREGIDO (ver docs/AUDITORIA_MAESTRA_2026-08-27.md,
            # seguimiento del 2026-08-31): antes se usaba `datetime.now()`
            # aca, es decir la hora de ESTE despacho, no la hora real del
            # dato. Eso asumia que spot y cadena de opciones siempre fallan
            # de forma atomica (cierto para Data912RestSource, que devuelve
            # (None, {}) para ambos a la vez ante un fallo de red) - pero es
            # FALSO para BrokerRestSource/IOL: se confirmo en una corrida
            # real que puede seguir "actualizando" el spot con normalidad
            # mientras la cadena de opciones lleva timeouts sostenidos, Y
            # TAMBIEN puede reproducir un spot cacheado viejo como si fuera
            # nuevo en cada poll. Usar `book.as_of` (la hora real en que la
            # fuente confirmo ese dato, ver OrderBookSnapshot.as_of /
            # live_shadow_feed.RawQuote.as_of) hace que esta guardia detecte
            # la antiguedad REAL del spot en vez de la cadencia de polling.
            self._spot_last_update_at = datetime.fromtimestamp(book.as_of, tz=timezone.utc)
            return
        self.option_chain.update_book(symbol, book)
        self._recent_volumes[symbol] = book.last_volume

    def _market_data_staleness_seconds(self, now: datetime) -> Optional[float]:
        """
        Segundos desde la ultima actualizacion exitosa del spot de GGAL, o
        None si todavia no llego ninguna (arranque - ya cubierto aparte por
        el chequeo `self._spot_book is None` en recompute_cycle()). `now`
        inyectable, mismo patron que el resto del ciclo (ver
        _run_weekly_asymmetric_cycle) para mantenerlo testeable.
        """
        if self._spot_last_update_at is None:
            return None
        return (now - self._spot_last_update_at).total_seconds()

    def _is_market_data_stale(self, now: datetime) -> bool:
        """
        Guardia de staleness (ver RiskConfig.max_market_data_staleness_seconds):
        True si la ultima actualizacion exitosa del spot de GGAL ya supera el
        umbral configurado - tipicamente por una caida de conectividad
        sostenida con la fuente de datos (data912.com caido, timeouts
        repetidos, websocket colgado sin desconectar formalmente), NO un
        fallo puntual de un unico poll (eso ya se resuelve solo en el
        siguiente ciclo sin intervencion). Devuelve False mientras todavia no
        llego ningun dato (recompute_cycle() ni siquiera llega a llamar a
        este metodo en ese caso).
        """
        staleness = self._market_data_staleness_seconds(now)
        if staleness is None:
            return False
        return staleness > SETTINGS.risk.max_market_data_staleness_seconds

    # -- Conexion y arranque -------------------------------------------------

    def connect_and_subscribe(self) -> bool:
        if self.shadow_mode:
            # Sin PyRofex, sin websocket: bootstrap_universe() arma el
            # universo (real via data912.com o sintetico via Mock/Replay) y
            # subscribe() es solo informativo. El refresco de datos ocurre
            # en cada recompute_cycle() via market_feed.poll() (ver abajo).
            self._subscribed_tickers = self.market_feed.bootstrap_universe(self.option_chain)
            self.market_feed.subscribe(self._subscribed_tickers)
            return True

        if not initialize_environment():
            logger.critical("No se pudo inicializar el ambiente de PyRofex. Abortando arranque.")
            return False

        self.ws_manager = WebSocketConnectionManager(
            market_data_handler=self.market_feed.handle_market_data,
            order_report_handler=self.order_gateway.on_order_report,
            on_reconnect=self._on_reconnect,
        )
        if not self.ws_manager.connect():
            logger.critical("No se pudo abrir el websocket de PyRofex. Abortando arranque.")
            return False

        self._subscribed_tickers = self.market_feed.bootstrap_universe(self.option_chain)
        self.market_feed.subscribe(self._subscribed_tickers)
        return True

    def _on_reconnect(self) -> None:
        """Tras una reconexion de websocket, hay que volver a suscribirse a los mismos tickers."""
        logger.info("Websocket reconectado: re-suscribiendo a %d instrumentos.", len(self._subscribed_tickers))
        if self._subscribed_tickers:
            self.market_feed.subscribe(self._subscribed_tickers)

    # -- Ciclo principal ----------------------------------------------------

    def recompute_cycle(self) -> None:
        """Un ciclo de calculo: IV/griegas -> señales -> riesgo -> hedge -> vigilancia de ordenes -> estado."""
        if self.shadow_mode:
            # En modo shadow no hay callbacks de websocket empujando datos:
            # se refresca la cadena explicitamente antes de recalcular. Esto
            # alimenta el mismo _on_book_update() que en modo real, asi que
            # el resto del ciclo (IV, griegas, señales, hedge) es identico.
            self.market_feed.poll(self.option_chain)

        if self._spot_book is None:
            logger.debug("Sin spot de GGAL todavia, se omite el ciclo.")
            return

        spot = self._spot_book.mid
        # `max_quote_age_seconds` (BUG REAL CORREGIDO, ver RiskConfig.
        # max_option_quote_staleness_seconds): evita recalcular IV/griegas
        # mezclando este `spot` FRESCO con el precio de una opcion VIEJO
        # (cadena de opciones caida sola mientras el spot sigue bien, ver
        # docstring de OrderBookSnapshot.as_of) - eso fabricaria un IV
        # internamente inconsistente que puede leerse como una dislocacion
        # de smile real sin serlo.
        stale_options = self.option_chain.recompute_all(
            spot=spot,
            rate=SETTINGS.rate.default_annual_rate,
            iv_calc=self.iv_calc,
            dividend_yield=SETTINGS.rate.dividend_yield,
            sigma_guess=SETTINGS.signal.iv_sigma_guess,
            max_quote_age_seconds=SETTINGS.risk.max_option_quote_staleness_seconds,
        )
        if stale_options:
            if not self._option_staleness_logged:
                logger.warning(
                    "ALERTA: %d opcion(es) con cotizacion de mas de %.0fs de antiguedad "
                    "(umbral configurado) - se excluyen del recalculo de IV/griegas y de "
                    "la deteccion de señales de entrada este ciclo (se sigue usando su "
                    "ultimo IV/griega conocido para posiciones ya abiertas).",
                    stale_options, SETTINGS.risk.max_option_quote_staleness_seconds,
                )
                self._option_staleness_logged = True
        elif self._option_staleness_logged:
            logger.info("Cotizaciones de opciones recuperadas: ninguna esta stale este ciclo.")
            self._option_staleness_logged = False

        if self.active_strategy_name == "vol_arbitrage":
            all_signals = self._run_vol_arbitrage_cycle(spot)
        else:
            all_signals = self._run_weekly_asymmetric_cycle(spot)

        totals = self.portfolio.total_greeks()
        if self.risk_manager.should_halt_new_positions(totals):
            logger.warning(self.risk_manager.breach_report(totals))

        self._maybe_hedge(totals, spot)

        # Vigilancia de ordenes abiertas: timeout, slippage y movimiento del subyacente.
        self.mid_price_exec.monitor_and_reprice(self._current_option_books(), spot)

        self.state_writer.write(
            portfolio_greeks_total=totals,
            portfolio_greeks_by_expiry=self.portfolio.greeks_by_expiry(),
            active_signals=[s.__dict__ for s in all_signals],
            risk_breaches=self.risk_manager.breach_report(totals),
            extra={"open_orders": self.mid_price_exec.open_order_count(), "spot_mid": spot},
            option_chain_snapshot=self._option_chain_snapshot(),
        )

    def _run_vol_arbitrage_cycle(self, spot: float) -> List[object]:
        """Ciclo bajo el modo original de arbitraje de volatilidad delta-neutral."""
        all_signals: List[object] = []
        for expiry, quotes in self.option_chain.quotes_by_expiry().items():
            # BUG REAL CORREGIDO (ver RiskConfig.max_option_quote_staleness_seconds):
            # `q.iv is not None` solo no alcanza - una opcion que quedo
            # excluida del recalculo por staleness (ver option_chain.
            # recompute_all()) sigue teniendo el ultimo IV que se le calculo
            # cuando todavia era fresca, que ya no es comparable contra el
            # resto de la sonrisa recalculada con el spot actual. Se excluye
            # explicitamente de la deteccion de señales (no de la cadena en
            # si: sigue disponible para portfolio/P&L con su ultimo valor).
            valid_quotes = [
                q for q in quotes
                if q.iv is not None
                and not q.book.is_stale(SETTINGS.risk.max_option_quote_staleness_seconds)
            ]
            if len(valid_quotes) < 3:
                continue
            surface = VolatilitySurface(valid_quotes)
            signals = self.strategy.scan_for_signals(surface, self._recent_volumes)
            all_signals.extend(signals)
            for s in signals:
                logger.info(
                    "Señal [%s]: %s %s (%.2f vol pts) - %s",
                    expiry, s.action, s.symbol, s.iv_dislocation_vol_points, s.reason,
                )
                self._act_on_signal(s, spot)
        return all_signals

    def _run_weekly_asymmetric_cycle(self, spot: float) -> List[object]:
        """
        Ciclo bajo el modo Long-First / Weekly Asymmetric (ver
        strategy/weekly_asymmetric.py). Orden deliberado, en cuatro pasos:

            0) TENDENCIA 1D (Analisis Tecnico - ver data/technical_analysis.py):
               se refresca (con cache propio por
               TechnicalAnalysisConfig.refresh_interval_seconds, tipicamente
               1h - no en cada ciclo de ~2s) el diagnostico BULLISH/BEARISH/
               NEUTRAL del grafico diario de GGAL. Este trend actua como
               filtro direccional OBLIGADO: se inyecta explicitamente en
               scan_entry_signals()/scan_spread_completion_signals() (nunca
               se computa dentro de weekly_asymmetric.py, que se mantiene
               libre de I/O - mismo patron de inyeccion que el `now` de
               evaluate_position_exit()).
            1) SALIDAS primero (evaluate_position_exit(), unica fuente de
               verdad de "cuando cerrar" - ver risk/risk_manager.py):
               Stop Loss, Take Profit, vencimiento del horizonte semanal y
               guardia de fin de semana se reconcilian ANTES de evaluar
               entradas nuevas en este mismo ciclo. Esto importa por dos
               razones: (a) libera capital comprometido (ver
               _capital_available_ars()) para que las entradas de este
               mismo ciclo dimensionen contra el capital ya liberado, y
               (b) evita evaluar una salida sobre una posicion recien
               abierta en el mismo tick (que siempre estaria dentro de
               banda de todos modos, pero el orden importa como invariante
               general del ciclo).
            2) ENTRADAS nuevas: solo señales de compra (buy_to_open),
               filtradas por la tendencia 1D del paso 0 y dimensionadas
               dinamicamente via risk/position_sizer.py.
            3) COMPLETAR SPREADS: la pata corta de un Bull Call/Bear Put
               Spread, unicamente sobre bases con una larga ya confirmada
               en el portafolio y consistente con la tendencia vigente
               (ver scan_spread_completion_signals).

        Entre los pasos 1 y 2 se evalua ademas una guardia de STALENESS de
        datos de mercado (ver RiskConfig.max_market_data_staleness_seconds,
        _is_market_data_stale()): si la ultima actualizacion del spot de
        GGAL supera el umbral configurado (caida sostenida de conectividad
        con la fuente de datos, no un fallo puntual de un unico poll), los
        pasos 2 y 3 se saltean por completo ese ciclo - no se toma exposicion
        nueva ni se completa un spread contra un precio que puede tener
        varios minutos de antiguedad. El paso 1 (salidas) y el delta-hedger
        SIGUEN activos durante la caida, con la ultima punta conocida: es
        preferible seguir gestionando riesgo ya tomado con un dato algo
        viejo que dejarlo completamente sin vigilancia.
        """
        now = datetime.now(timezone.utc)
        all_signals: List[object] = []

        # -- 0) Tendencia 1D (filtro direccional obligado) ----------------------
        trend = Trend.NEUTRAL.value
        momentum_shift: Optional[str] = None
        if self.technical_engine is not None:
            try:
                snapshot = self.technical_engine.refresh(now=now)
                trend = snapshot.trend.value if hasattr(snapshot.trend, "value") else snapshot.trend
                # Momentum Shift / Early Reversal Override (ver
                # data/technical_analysis.py:MomentumShift): mismo
                # TechnicalSnapshot que `trend`, inyectado igual que `trend`
                # en scan_entry_signals() - ver docstring de ese metodo.
                momentum_shift = snapshot.momentum_shift
                # refresh() devuelve el MISMO objeto (misma identidad) mientras
                # el cache siga vigente (ver refresh_interval_seconds, tipicamente
                # 1h) - se loguea solo cuando cambia la instancia (o sea, cuando
                # hubo un recalculo real), para no repetir la misma linea en
                # cada ciclo de ~2-4s del bot durante una hora entera.
                if snapshot is not self._last_ta_snapshot_logged:
                    logger.info(
                        "Tendencia 1D GGAL: %s (%s) [fuente=%s, velas=%d]",
                        trend, snapshot.reason, snapshot.data_source, snapshot.bars_used,
                    )
                    if momentum_shift:
                        logger.info(
                            "Momentum Shift detectado: %s - se relaja el bloqueo del tipo de opcion contrario "
                            "a '%s' bajo umbral EXTREMO de dislocacion de smile (ver TechnicalAnalysisConfig).",
                            momentum_shift, trend,
                        )
                    self._last_ta_snapshot_logged = snapshot
            except Exception:
                # Un fallo en el Analisis Tecnico (ej. data912 caido y sin
                # fallback sintetico disponible) no debe tumbar el ciclo
                # entero: se degrada a NEUTRAL (el mas conservador de los
                # tres estados - exige dislocacion extrema para entrar y
                # nunca completa spreads) y se sigue.
                logger.exception("Error refrescando la tendencia 1D; se degrada a NEUTRAL este ciclo.")

        # -- 1) Salidas primero -------------------------------------------------
        current_prices = {
            q.symbol: q.book.mid for q in self.option_chain.all_quotes()
            if q.book.bid > 0 and q.book.ask > 0
        }
        # Griegas vigentes por simbolo (para la salida por compresion de
        # vega - ver risk_manager.evaluate_vega_decay_exit): se recalculan
        # arriba en option_chain.recompute_all() al inicio de este mismo
        # ciclo, asi que ya reflejan el spot/tiempo actual, no el de la
        # entrada (esa base de comparacion es Position.greeks_per_unit,
        # congelada al fill).
        current_greeks = {q.symbol: q.greeks for q in self.option_chain.all_quotes() if q.greeks is not None}
        exit_signals = self.strategy.build_exit_signals(self.portfolio, current_prices, now, current_greeks=current_greeks)
        all_signals.extend(exit_signals)
        for ex in exit_signals:
            logger.info("Salida [Long-First]: %s %s x%.2f - %s", ex.action, ex.symbol, ex.quantity, ex.reason)
            self._act_on_exit_signal(ex, spot)

        # -- 1.5) Guardia de staleness de datos de mercado ----------------------
        # Ver RiskConfig.max_market_data_staleness_seconds / _is_market_data_stale().
        # Deliberadamente DESPUES de las salidas (paso 1, arriba) y ANTES de
        # las entradas/spreads (pasos 2-3, abajo): una posicion ya abierta
        # sigue gestionandose con la ultima punta conocida (mejor eso que
        # dejarla completamente sin vigilancia), pero NO se toma exposicion
        # nueva ni se completa un spread contra un dato que puede tener
        # varios minutos de antiguedad.
        market_data_stale = self._is_market_data_stale(now)
        if market_data_stale:
            if not self._market_data_stale_logged:
                staleness = self._market_data_staleness_seconds(now)
                logger.warning(
                    "ALERTA: datos de mercado con %.0fs de antiguedad (umbral=%.0fs) - "
                    "se pausan ENTRADAS nuevas y armado de spreads hasta que vuelva a "
                    "haber una actualizacion reciente del spot de GGAL. Las salidas "
                    "(Stop Loss/Take Profit/etc.) y el delta-hedger siguen activos.",
                    staleness, SETTINGS.risk.max_market_data_staleness_seconds,
                )
                self._market_data_stale_logged = True
        elif self._market_data_stale_logged:
            logger.info("Datos de mercado recuperados: se reanudan entradas nuevas y armado de spreads.")
            self._market_data_stale_logged = False

        # -- 2) Entradas nuevas (recien despues de reconciliar salidas) ---------
        if not market_data_stale:
            for expiry, quotes in self.option_chain.quotes_by_expiry().items():
                # Ver comentario equivalente en _run_vol_arbitrage_cycle:
                # una opcion excluida del recalculo por staleness (option_chain.
                # recompute_all()) conserva su ultimo IV conocido, que ya no es
                # comparable contra el resto de la sonrisa recalculada con el
                # spot actual - se excluye de la deteccion de señales de entrada.
                valid_quotes = [
                    q for q in quotes
                    if q.iv is not None
                    and not q.book.is_stale(SETTINGS.risk.max_option_quote_staleness_seconds)
                ]
                if len(valid_quotes) < 3:
                    continue
                surface = VolatilitySurface(valid_quotes)
                entry_signals = self.strategy.scan_entry_signals(
                    surface, self._recent_volumes, trend=trend, momentum_shift=momentum_shift,
                )
                all_signals.extend(entry_signals)
                for es in entry_signals:
                    logger.info(
                        "Señal [%s]: %s %s (%.2f vol pts, score conv.=%.4f) - %s",
                        expiry, es.action, es.symbol, es.iv_dislocation_vol_points, es.convexity_score, es.reason,
                    )
                    self._act_on_entry_signal(es, spot)

            # -- 3) Completar spreads: pata corta solo tras la larga confirmada -
            if SETTINGS.long_first.enable_spread_completion:
                # BUG REAL CORREGIDO (ver RiskConfig.max_option_quote_staleness_seconds
                # y el docstring de WeeklyAsymmetricStrategy.scan_spread_completion_signals/
                # _find_wing_quote): cierra el ultimo hueco del "paso 3" - antes se
                # completaba un spread contra el "wing" mas cercano sin mirar si su
                # cotizacion estaba stale, a diferencia de las entradas del paso 2
                # (que ya excluyen opciones stale de valid_quotes mas arriba).
                spread_signals = self.strategy.scan_spread_completion_signals(
                    self.option_chain, self.portfolio, trend=trend,
                    max_quote_age_seconds=SETTINGS.risk.max_option_quote_staleness_seconds,
                    now=time.time(),
                )
                all_signals.extend(spread_signals)
                for sp in spread_signals:
                    logger.info(
                        "Spread [Long-First]: %s sobre %s (cubre %s) - %s",
                        sp.action, sp.short_symbol, sp.long_symbol, sp.reason,
                    )
                    self._act_on_spread_completion_signal(sp, spot)

        return all_signals

    def _position_quantity(self, symbol: str) -> float:
        """Posicion neta (signed) actualmente registrada en self.portfolio para `symbol`."""
        return sum(p.quantity for p in self.portfolio.positions if p.symbol == symbol)

    def _capital_available_ars(self) -> float:
        """
        Capital libre para nuevas entradas bajo el modo Long-First: el techo
        configurado (`LongFirstConfig.max_capital_ars`) menos lo ya
        comprometido en posiciones LARGAS DE OPCIONES abiertas, valuado a su
        propio precio de ENTRADA (no a mercado - ver risk/position_sizer.py,
        que dimensiona contra capital comprometido, no contra PnL flotante).
        Nunca negativo.

        Se excluye explicitamente la posicion del subyacente que deja el
        delta-hedger (ver _maybe_hedge(): `greeks_per_unit is None` es la
        marca de "esto es el subyacente, no una opcion" - ver
        portfolio.Position). El presupuesto de capital de este modo es para
        comprar CONVEXIDAD (opciones), no para la cobertura de delta, que es
        una decision de riesgo separada y no deberia competir por el mismo
        presupuesto ni reducir el sizing de la proxima señal de entrada.
        """
        committed = sum(
            pos.quantity * (pos.entry_price or 0.0) * pos.multiplier
            for pos in self.portfolio.positions
            if pos.quantity > 0 and pos.entry_price is not None and pos.greeks_per_unit is not None
        )
        return max(0.0, SETTINGS.long_first.max_capital_ars - committed)

    def _option_chain_snapshot(self) -> List[Dict]:
        """
        Volcado de la cadena de opciones vigente (puntas, IV, griegas) para
        persistir en state/bot_state.json. Solo lo consume dashboard/ (ver
        dashboard/pnl_engine.py) para marcar a mercado posiciones abiertas y
        graficar el smile de IV; el motor de trading no lo relee.
        """
        snapshot = []
        for q in self.option_chain.all_quotes():
            snapshot.append({
                "symbol": q.symbol,
                "strike": q.strike,
                "expiry": q.expiry.isoformat(),
                "option_type": q.option_type.value,
                "bid": q.book.bid,
                "ask": q.book.ask,
                "mid": q.book.mid,
                "last_volume": q.book.last_volume,
                "iv": q.iv,
                "spot_ref": q.spot_ref,
                "days_calendar": q.days_calendar,
                "days_business": q.days_business,
                "greeks": q.greeks,
            })
        return snapshot

    def _act_on_signal(self, signal, spot: float) -> None:
        """
        Arma la orden delta-neutral de la señal de arbitraje: la pata de la
        opcion (a mid-price, via MidPriceExecutionEngine) y, si el delta
        resultante saca al portafolio de la banda, la cobertura se dispara
        en el proximo _maybe_hedge() del mismo ciclo (no aca), para
        rehedgear sobre el delta TOTAL de la cuenta y no operacion por
        operacion (evita sobre-operar el subyacente).

        IMPORTANTE (bug real detectado corriendo el bot en modo shadow): la
        señal de smile_dislocation persiste mientras la sonrisa no se
        corrija, así que scan_for_signals() la va a re-emitir en TODOS los
        ciclos siguientes. Sin las dos guardas de abajo, el bot reentraba la
        MISMA base una y otra vez (una orden nueva cada ciclo, sin límite),
        porque nunca quedaba registro de que ya se había operado esa señal.
        Las guardas son deliberadamente simples (una base por vez, sin
        pyramideo) - no implementan una logica de salida/take-profit; cerrar
        la posicion cuando la sonrisa se normalice sigue siendo un TODO
        aparte (ver README).
        """
        quote = self.option_chain.get(signal.symbol)
        if quote is None or quote.book.bid <= 0 or quote.book.ask <= 0:
            return

        # Guarda 1: ya hay una orden de esta misma base en vigilancia
        # (todavia sin fill/cancel resuelto) - no duplicar la exposicion
        # antes de saber que paso con la primera.
        if self.mid_price_exec.has_open_order_for(signal.symbol):
            logger.debug("Señal %s ignorada: ya hay una orden en vigilancia sobre esa base.", signal.symbol)
            return

        # Guarda 2: ya existe una posicion abierta (fill previo) sobre esta
        # base - no pyramidear sobre la misma señal en cada ciclo. Esta
        # cuenta depende de que el fill haya sido reconciliado en
        # self.portfolio mas abajo (tanto en modo shadow, donde el fill es
        # instantaneo, como en real via order reports/get_account_positions,
        # a completar segun la integracion final con tu ALYC).
        if self._position_quantity(signal.symbol) != 0:
            logger.debug("Señal %s ignorada: ya existe una posicion abierta sobre esa base.", signal.symbol)
            return

        totals = self.portfolio.total_greeks()
        if self.risk_manager.should_halt_new_positions(totals):
            logger.info("Señal %s descartada: la cuenta ya excede limites de riesgo.", signal.symbol)
            return

        side = OrderSide.SELL if signal.action == "sell" else OrderSide.BUY
        # TODO: dimensionar `quantity` segun el tamaño de cuenta y el limite
        # de riesgo disponible (cuanta vega/gamma queda antes de tocar
        # RiskLimits), no un tamaño fijo. 1 contrato (100 opciones) es un
        # placeholder conservador para arrancar en paper trading.
        quantity = 1
        state = self.mid_price_exec.submit(
            symbol=signal.symbol, book=quote.book, side=side, quantity=quantity,
            spot_reference=spot, aggressive=False,
        )

        # Reconciliacion inmediata para el caso shadow (fill sincronico: ver
        # execution/order_gateway.py, SETTINGS.shadow.enabled) y para
        # cualquier otro caso donde send() ya haya devuelto FILLED (ej. una
        # orden agresiva que cruzo el spread). Un fill asincronico real (via
        # WebSocketConnectionManager -> order_gateway.on_order_report) NO
        # pasa por aca todavia: reconciliarlo ahi (o via
        # get_account_positions()) sigue pendiente de la integracion final
        # con tu ALYC, y sin eso la Guarda 2 no sirve fuera de modo shadow.
        if state.status is OrderStatus.FILLED and quote.greeks is not None:
            signed_qty = quantity if side is OrderSide.BUY else -quantity
            self.portfolio.add(Position(
                symbol=signal.symbol, quantity=signed_qty,
                multiplier=SETTINGS.instruments.option_multiplier,
                greeks_per_unit=quote.greeks, expiry=quote.expiry,
                # entry_price/entry_time: metadata para el modo Long-First
                # (ver risk.risk_manager.RiskManager.evaluate_position_exit
                # y strategy/weekly_asymmetric.py) - se pobla aca sin
                # importar que estrategia este activa, porque sin esto
                # ningun Stop Loss/Take Profit/horizonte semanal es evaluable.
                entry_price=state.avg_fill_price, entry_time=datetime.now(timezone.utc),
            ))

    def _act_on_exit_signal(self, signal, spot: float) -> None:
        """
        Cierra (sell_to_close) la posicion larga que disparo la señal de
        salida (ver risk.risk_manager.RiskManager.evaluate_position_exit,
        unica fuente de verdad de "cuando cerrar" bajo el modo Long-First).

        Igual que _act_on_signal()/_act_on_entry_signal() (modo
        vol_arbitrage y weekly_asymmetric respectivamente), este metodo
        asume a lo sumo un lote abierto por base (sin pyramideo - la Guarda
        2 de _act_on_entry_signal() es la que sostiene esa invariante en
        este mismo modo): por eso, tras el fill, alcanza con vaciar a 0
        todas las posiciones largas de ese simbolo, sin necesitar trackear
        que lote especifico genero la señal.
        """
        quote = self.option_chain.get(signal.symbol)
        if quote is None or quote.book.bid <= 0 or quote.book.ask <= 0:
            logger.warning("Salida %s no ejecutable este ciclo: sin punta operable.", signal.symbol)
            return

        if self.mid_price_exec.has_open_order_for(signal.symbol):
            logger.debug("Salida %s pospuesta: ya hay una orden en vigilancia sobre esa base.", signal.symbol)
            return

        state = self.mid_price_exec.submit(
            symbol=signal.symbol, book=quote.book, side=OrderSide.SELL, quantity=signal.quantity,
            spot_reference=spot, aggressive=False,
        )

        if state.status is OrderStatus.FILLED:
            for pos in self.portfolio.positions:
                if pos.symbol == signal.symbol and pos.quantity > 0:
                    pos.quantity = 0.0

    def _act_on_entry_signal(self, signal, spot: float) -> None:
        """
        Compra (buy_to_open) la EntrySignal de WeeklyAsymmetricStrategy,
        dimensionando la cantidad de contratos via risk/position_sizer.py
        contra el capital disponible (ver _capital_available_ars()) en vez
        de un tamaño fijo. Mismas guardas anti-reentrada que _act_on_signal
        (modo vol_arbitrage): no duplicar sobre una orden en vigilancia ni
        sobre una posicion ya abierta en esa base.
        """
        quote = self.option_chain.get(signal.symbol)
        if quote is None or quote.book.bid <= 0 or quote.book.ask <= 0:
            return

        # Guarda 1: orden de esta base ya en vigilancia.
        if self.mid_price_exec.has_open_order_for(signal.symbol):
            logger.debug("Señal %s ignorada: ya hay una orden en vigilancia sobre esa base.", signal.symbol)
            return

        # Guarda 2: ya existe una posicion abierta sobre esta base (sin pyramideo).
        if self._position_quantity(signal.symbol) != 0:
            logger.debug("Señal %s ignorada: ya existe una posicion abierta sobre esa base.", signal.symbol)
            return

        totals = self.portfolio.total_greeks()
        if self.risk_manager.should_halt_new_positions(totals):
            logger.info("Señal %s descartada: la cuenta ya excede limites de riesgo.", signal.symbol)
            return

        sizing = self.position_sizer.compute_contracts(
            premium_price=signal.premium_reference,
            capital_available_ars=self._capital_available_ars(),
        )
        if not sizing.is_tradeable:
            logger.info("Señal %s descartada por sizing (%s).", signal.symbol, sizing.rejected_reason)
            return

        state = self.mid_price_exec.submit(
            symbol=signal.symbol, book=quote.book, side=OrderSide.BUY, quantity=sizing.contracts,
            spot_reference=spot, aggressive=False,
        )

        if state.status is OrderStatus.FILLED and quote.greeks is not None:
            self.portfolio.add(Position(
                symbol=signal.symbol, quantity=sizing.contracts,
                multiplier=SETTINGS.instruments.option_multiplier,
                greeks_per_unit=quote.greeks, expiry=quote.expiry,
                entry_price=state.avg_fill_price, entry_time=datetime.now(timezone.utc),
            ))

    def _act_on_spread_completion_signal(self, signal, spot: float) -> None:
        """
        Vende (sell_to_open_wing) la pata corta de un spread (Bull Call /
        Bear Put) YA financiado por una larga confirmada - ver
        WeeklyAsymmetricStrategy.scan_spread_completion_signals, cuya
        invariante de codigo garantiza que nunca se llega aca sin esa larga
        ya en el portafolio. La cantidad de la pata corta replica 1:1 la
        cantidad larga confirmada (spread simple, sin ratio).
        """
        quote = self.option_chain.get(signal.short_symbol)
        if quote is None or quote.book.bid <= 0 or quote.book.ask <= 0:
            return
        if self.mid_price_exec.has_open_order_for(signal.short_symbol):
            logger.debug("Pata corta %s pospuesta: ya hay una orden en vigilancia sobre esa base.", signal.short_symbol)
            return
        if self._position_quantity(signal.short_symbol) != 0:
            logger.debug("Pata corta %s ignorada: ya existe una posicion sobre esa base.", signal.short_symbol)
            return

        quantity = signal.long_quantity_confirmed
        state = self.mid_price_exec.submit(
            symbol=signal.short_symbol, book=quote.book, side=OrderSide.SELL, quantity=quantity,
            spot_reference=spot, aggressive=False,
        )

        if state.status is OrderStatus.FILLED and quote.greeks is not None:
            self.portfolio.add(Position(
                symbol=signal.short_symbol, quantity=-quantity,
                multiplier=SETTINGS.instruments.option_multiplier,
                greeks_per_unit=quote.greeks, expiry=quote.expiry,
                entry_price=state.avg_fill_price, entry_time=datetime.now(timezone.utc),
            ))

    def _maybe_hedge(self, totals: Dict[str, float], spot: float) -> None:
        """
        BUG REAL CORREGIDO (reportado por el usuario: el dashboard mostraba
        una posicion de delta-hedge de ~38.000 acciones de GGAL y un PnL no
        realizado de ~$17 millones): hasta esta correccion, el fill de la
        orden de cobertura NUNCA se registraba en self.portfolio. Como
        needs_hedge()/execute_hedge() deciden cuanto cubrir en base a
        `totals["delta"]` (que sale de self.portfolio.total_greeks()), y esa
        cobertura recien ejecutada nunca quedaba reflejada ahi, CADA ciclo
        siguiente volvia a ver el mismo delta "fuera de banda" y disparaba
        OTRA orden de cobertura del mismo tamaño - un rehedge sin fin, sin
        limite, cada ~2-4s, acumulando una posicion del subyacente cada vez
        mas grande sin que ninguna de esas ordenes redujera jamas el delta
        que el bot creia tener. En modo real (no shadow) esto habria sido
        una posicion direccional descontrolada con dinero real. Ver
        test_execution_pipeline.py, test_maybe_hedge_records_fill_so_delta_reflects_the_hedge.
        """
        if not self.delta_hedger.needs_hedge(totals["delta"]):
            return
        if self._spot_book is None:
            return
        futuro_book = self._current_option_books().get(SETTINGS.instruments.futuro_ticker) if SETTINGS.instruments.futuro_ticker else None
        state = self.delta_hedger.execute_hedge(
            portfolio_delta=totals["delta"],
            contado_book=self._spot_book,
            futuro_book=futuro_book,
            mid_price_engine=self.mid_price_exec,
            min_size=SETTINGS.risk.hedge_min_size,
            max_spread_relative=SETTINGS.risk.hedge_max_spread_relative,
        )
        if state is None:
            logger.warning(
                "Delta fuera de banda (%.2f) pero ninguna ruta de hedge es operable. "
                "Requiere intervencion manual.", totals["delta"],
            )
            return

        if state.status is OrderStatus.FILLED:
            signed_qty = state.request.quantity if state.request.side is OrderSide.BUY else -state.request.quantity
            self.portfolio.add(Position(
                symbol=state.request.symbol, quantity=signed_qty,
                # multiplier=1.0: el subyacente cotiza por ACCION, no por
                # contrato de opciones de 100 unidades (ver el mismo ajuste
                # en dashboard/pnl_engine.py, multiplier_for_symbol()).
                multiplier=1.0,
                # greeks_per_unit=None es la marca (ver portfolio.Position y
                # _capital_available_ars()) de "esto es el subyacente,
                # delta=1 por unidad" - nunca se confunde con una opcion.
                greeks_per_unit=None,
                entry_price=state.avg_fill_price, entry_time=datetime.now(timezone.utc),
            ))

    def _current_option_books(self) -> Dict[str, OrderBookSnapshot]:
        books = {q.symbol: q.book for q in self.option_chain.all_quotes()}
        if self._spot_book is not None:
            books[SETTINGS.instruments.contado_ticker] = self._spot_book
        return books

    # -- Ciclo de vida --------------------------------------------------------

    def run_forever(self, cycle_seconds: float = 2.0) -> None:
        logger.info("Iniciando GgalOptionsBot en ambiente %s", SETTINGS.broker.environment)
        if not self.connect_and_subscribe():
            sys.exit(1)

        self._install_signal_handlers()
        try:
            while not self._shutting_down:
                try:
                    self.recompute_cycle()
                except Exception:
                    # Un error en un ciclo no debe tumbar el proceso: se loguea
                    # y se sigue, pero si se repite persistentemente el
                    # RiskManager/alertas externas deben notarlo (ver breach_report).
                    logger.exception("Error no controlado en recompute_cycle(); se continua en el proximo ciclo.")
                time.sleep(cycle_seconds)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):  # noqa: ARG001
            logger.info("Señal de apagado recibida (%s); iniciando graceful shutdown.", signum)
            self._shutting_down = True

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def shutdown(self) -> None:
        """Graceful shutdown: cancela ordenes abiertas, cierra el websocket y persiste el estado final."""
        logger.info("Apagando GgalOptionsBot...")
        try:
            self.order_gateway.cancel_all_open()
        except Exception:
            logger.exception("Error cancelando ordenes abiertas durante el shutdown.")

        if self.ws_manager is not None:
            try:
                self.ws_manager.close()
            except Exception:
                logger.exception("Error cerrando el websocket durante el shutdown.")

        try:
            totals = self.portfolio.total_greeks()
            # Se preserva el ultimo snapshot de la cadena/spot conocido (en
            # vez de dejarlo vacio) para que el dashboard pueda seguir
            # marcando a mercado las posiciones abiertas justo despues de
            # que el bot se detuvo, no solo mientras esta corriendo.
            spot_mid = self._spot_book.mid if self._spot_book is not None else None
            self.state_writer.write(
                portfolio_greeks_total=totals,
                portfolio_greeks_by_expiry=self.portfolio.greeks_by_expiry(),
                active_signals=[],
                risk_breaches=self.risk_manager.breach_report(totals),
                extra={"shutdown": True, "spot_mid": spot_mid},
                option_chain_snapshot=self._option_chain_snapshot(),
            )
        except Exception:
            logger.exception("Error escribiendo el estado final durante el shutdown.")

        logger.info("Shutdown completo.")


if __name__ == "__main__":
    bot = GgalOptionsBot()
    bot.run_forever()
