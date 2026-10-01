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
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from ggal_bot.config import SETTINGS, VALID_STRATEGIES
from ggal_bot import market_hours
from ggal_bot.paths import LOG_FILE
from ggal_bot.data.market_data_feed import MarketDataFeed
from ggal_bot.data.live_shadow_feed import LiveShadowFeed
from ggal_bot.data.option_chain import OptionChain, OrderBookSnapshot
from ggal_bot.models.implied_vol import ImpliedVolatilityCalculator
from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.portfolio.portfolio import Portfolio, Position
from ggal_bot.portfolio.event_journal import PositionEventJournal
from ggal_bot.data.market_snapshot_log import MarketSnapshotLogger
from ggal_bot.data.signal_funnel_log import SignalFunnelLogger
from ggal_bot.data.ccl_bond_quote_log import CclBondQuoteLogger
from ggal_bot.env_introspection import list_ggal_bot_env_vars
from ggal_bot.version_info import get_deployed_git_sha
from ggal_bot.data.dislocation_history import DislocationHistoryTracker
from ggal_bot.portfolio.reconciliation import (
    ReconciliationUnavailable,
    reconstruct_positions_from_event_journal,
)
from ggal_bot.risk.risk_manager import RiskLimits, RiskManager
from ggal_bot.risk.position_sizer import PositionSizer
from ggal_bot.risk.kill_switch import KillSwitch
from ggal_bot.risk import invariants
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
from ggal_bot.strategy.weekly_asymmetric import EntryScanDiagnostics, ExitSignal, WeeklyAsymmetricStrategy
from ggal_bot.strategy.scalping import ScalpingStrategy
from ggal_bot.data.technical_analysis import TechnicalAnalysisEngine, Trend
from ggal_bot.data.intraday_bars import MultiTimeframeIntradayEngine
from ggal_bot.state_writer import StateWriter

def _art_time_converter(seconds: float) -> time.struct_time:
    """
    Convierte epoch seconds a hora de Argentina (ART, UTC-3 fijo, sin
    horario de verano desde 2009 - mismo criterio y mismo offset que
    risk.risk_manager.RiskManager._is_past_eod, sin agregar una dependencia
    de zoneinfo/pytz solo para esto) para que %(asctime)s en los logs
    muestre la hora local de Argentina en vez de la hora del servidor/
    contenedor (que en Northflank corre en UTC por defecto - ver
    deploy/). Se asigna como atributo de INSTANCIA del Formatter de abajo
    (nunca de clase - logging.Formatter.converter, si fuera una funcion
    Python comun asignada a nivel de clase, se convertiria en metodo ligado
    y recibiria `self` como primer argumento en vez de los segundos epoch;
    time.localtime/time.gmtime evitan ese problema en la stdlib solo
    porque son built-ins de C, no funciones Python).
    """
    return time.gmtime(seconds - 3 * 3600)


logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
_art_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_art_log_formatter.converter = _art_time_converter
for _handler in logging.root.handlers:
    _handler.setFormatter(_art_log_formatter)

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
        # Seleccion EXCLUYENTE de estrategia activa (ver config.
        # StrategyConfig/VALID_STRATEGIES y su advertencia de diseño
        # completa): "weekly_asymmetric" (Long-First, DEFAULT), "vol_arbitrage"
        # (arbitraje delta-neutral original) o "scalping" (Scalping Intradia
        # como PRINCIPAL, no aditivo - agregado 2026-09-07 a pedido explicito
        # del usuario). Un valor invalido no frena el arranque - cae al
        # default con una advertencia explicita, en vez de fallar en
        # silencio o crashear el proceso.
        active_strategy_name = SETTINGS.strategy.active
        if active_strategy_name not in VALID_STRATEGIES:
            logger.warning(
                "GGAL_BOT_ACTIVE_STRATEGY=%r no es valido (opciones: %s); se usa "
                "'weekly_asymmetric' por defecto.",
                active_strategy_name, ", ".join(VALID_STRATEGIES),
            )
            active_strategy_name = "weekly_asymmetric"

        # SALVAGUARDA (TANDA 2 "OPTIMIZACION EJECUTABLE", seccion 12,
        # 2026-09-08): vol_arbitrage es NO-GO de produccion por instruccion
        # explicita y repetida del usuario (debe permanecer SHADOW/PAPER
        # ONLY - nunca cierra posiciones solo, ver TODO documentado en
        # _act_on_signal). VERIFICADO por lectura de codigo: antes de esta
        # salvaguarda, NADA en VALID_STRATEGIES/la seleccion de arriba
        # distinguia "shadow-only" de "production-ready" - un
        # GGAL_BOT_ACTIVE_STRATEGY=vol_arbitrage puesto por error (o por
        # configuracion vieja arrastrada) en un deploy CON
        # GGAL_BOT_SHADOW_MODE=false habria operado vol_arbitrage con
        # ordenes REALES sin que ningun otro chequeo lo impidiera. Se
        # verifica aca, temprano en el arranque, contra SETTINGS.shadow.
        # enabled directamente (no contra self.shadow_mode, todavia sin
        # asignar en este punto del __init__): si se pide vol_arbitrage
        # fuera de modo shadow, se cae a "weekly_asymmetric" con un
        # logger.critical explicito, en vez de arrancar en silencio con
        # exposicion short/vol_arbitrage real no autorizada.
        if active_strategy_name == "vol_arbitrage" and not SETTINGS.shadow.enabled:
            logger.critical(
                "GGAL_BOT_ACTIVE_STRATEGY=vol_arbitrage pero GGAL_BOT_SHADOW_MODE=false "
                "(o sin setear): vol_arbitrage es NO-GO DE PRODUCCION (debe permanecer "
                "SHADOW/PAPER ONLY, no cierra posiciones solo - ver TODO en _act_on_signal). "
                "Se fuerza 'weekly_asymmetric' en su lugar. Para operar vol_arbitrage, hacerlo "
                "UNICAMENTE con GGAL_BOT_SHADOW_MODE=true."
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
        # `self.strategy`: ver VALID_STRATEGIES/StrategyConfig en config.py
        # para la advertencia de diseño completa sobre GGAL_BOT_ACTIVE_
        # STRATEGY=scalping (seleccion EXCLUYENTE, 2026-09-07, a pedido
        # explicito del usuario). Queda en None bajo "scalping": ese modo
        # usa self.scalping_strategy (ver bloque de Scalping mas abajo, que
        # se fuerza a ENCENDIDO cuando este es el valor activo), no una
        # instancia nueva aca - self.strategy solo se referencia dentro de
        # _run_weekly_asymmetric_cycle/_run_vol_arbitrage_cycle, y ninguna
        # de las dos se llama desde recompute_cycle() cuando el valor activo
        # es "scalping" (ver ahi), asi que None nunca se dereferencia.
        self.strategy: Optional[object] = None
        if self.active_strategy_name == "vol_arbitrage":
            self.strategy = VolatilityArbitrageStrategy(
                self.risk_manager, smile_threshold_vol_points=SETTINGS.signal.smile_threshold_vol_points,
            )
        elif self.active_strategy_name == "scalping":
            pass  # ver comentario de self.strategy arriba y el bloque de Scalping mas abajo.
        else:  # "weekly_asymmetric" (default)
            self.strategy = WeeklyAsymmetricStrategy(self.risk_manager, config=SETTINGS.long_first)
            self.position_sizer = PositionSizer()
            self.technical_engine = TechnicalAnalysisEngine(config=SETTINGS.technical_analysis)
            # Ventana rodante de dislocacion de smile por simbolo + z-score
            # (MEJORA 2026-09-28, ver config.LongFirstConfig.
            # enable_zscore_filter y data/dislocation_history.py) -
            # INDEPENDIENTE de la de Scalping (self.scalping_iv_tracker mas
            # abajo, si existe), nunca compartida.
            self._dislocation_tracker = DislocationHistoryTracker(
                max_window_seconds=SETTINGS.long_first.zscore_window_seconds,
                min_samples=SETTINGS.long_first.zscore_min_samples,
            )
        # Ultimo TechnicalSnapshot ya logueado (por identidad de objeto, ver
        # _run_weekly_asymmetric_cycle) - evita repetir la misma linea de
        # "Tendencia 1D GGAL: ..." en cada ciclo mientras el cache del motor
        # tecnico siga vigente.
        self._last_ta_snapshot_logged = None
        # Throttle del log de diagnostico de escaneo de entradas (ver
        # strategy.weekly_asymmetric.EntryScanDiagnostics): el ciclo corre
        # cada ~2s (run_forever), asi que sin este throttle el log de
        # diagnostico saturaria logs/ggal_bot.log. Agregado a pedido
        # explicito (ver seguimiento de auditoria del 2026-09-01) para poder
        # ver, con datos reales, que tan lejos estan las bases candidatas
        # del umbral de dislocacion vigente - sin cambiar ningun umbral.
        self._last_entry_diagnostics_logged_at: Optional[float] = None
        self._entry_diagnostics_log_interval_seconds: float = 300.0
        logger.info("Estrategia activa: %s", self.active_strategy_name)

        # Vencimiento forzado (a pedido explicito del usuario, 2026-09-01 -
        # ver InstrumentsConfig.forced_expiry): si esta seteado, se ignora
        # por completo cualquier OTRO vencimiento, tanto para entradas
        # nuevas (ver _run_weekly_asymmetric_cycle) como para completar
        # spreads/wings (ver scan_spread_completion_signals). Se valida y
        # se loguea UNA sola vez aca, no en cada ciclo.
        raw_forced_expiry = SETTINGS.instruments.forced_expiry
        self._forced_expiry = SETTINGS.instruments.forced_expiry_date()
        if raw_forced_expiry.strip() and self._forced_expiry is None:
            logger.warning(
                "GGAL_BOT_FORCE_EXPIRY=%r no se pudo interpretar como fecha ISO (YYYY-MM-DD) - se "
                "ignora, el bot sigue operando todos los vencimientos elegibles segun el horizonte "
                "semanal (comportamiento normal).", raw_forced_expiry,
            )
        elif self._forced_expiry is not None:
            # GGAL_BOT_MAX_HOLDING_BUSINESS_DAYS puede ser None ("sin limite",
            # ver LongFirstConfig en config.py, AJUSTE 2026-09-07) - en ese
            # caso el vencimiento forzado nunca queda bloqueado por este
            # motivo, asi que la advertencia de abajo no aplica.
            horizon = SETTINGS.long_first.max_holding_business_days
            if horizon is not None:
                logger.warning(
                    "Vencimiento FORZADO por config (GGAL_BOT_FORCE_EXPIRY=%s): el bot va a ignorar "
                    "cualquier otro vencimiento por completo, tanto para entradas nuevas como para "
                    "completar spreads - verificar que GGAL_BOT_MAX_HOLDING_BUSINESS_DAYS (hoy=%d dias "
                    "habiles) cubra el plazo real hasta ese vencimiento, o ninguna entrada va a poder "
                    "abrirse ahi.", self._forced_expiry.isoformat(), horizon,
                )
            else:
                logger.warning(
                    "Vencimiento FORZADO por config (GGAL_BOT_FORCE_EXPIRY=%s): el bot va a ignorar "
                    "cualquier otro vencimiento por completo, tanto para entradas nuevas como para "
                    "completar spreads. GGAL_BOT_MAX_HOLDING_BUSINESS_DAYS esta en 'sin limite', asi "
                    "que este vencimiento nunca queda bloqueado por horizonte de entrada.",
                    self._forced_expiry.isoformat(),
                )

        self.delta_hedger = DeltaHedgingEngine(delta_band=SETTINGS.risk.delta_band)

        # A pedido explicito del usuario (2026-09-01, ver
        # RiskConfig.enable_delta_hedge / GGAL_BOT_ENABLE_DELTA_HEDGE): con
        # este flag en false, _maybe_hedge() no dispara NINGUNA orden sobre
        # el subyacente/futuro - el bot opera solo opciones. Se valida y
        # loguea UNA sola vez aca, no en cada ciclo.
        if not SETTINGS.risk.enable_delta_hedge:
            logger.warning(
                "Delta-hedging DESACTIVADO por config (GGAL_BOT_ENABLE_DELTA_HEDGE=false): el bot "
                "NUNCA va a operar el subyacente/futuro de GGAL para neutralizar delta, sin importar "
                "cuanto delta direccional acumule la cartera de opciones - a pedido explicito del "
                "usuario, no hay ningun tope de reemplazo que bloquee nuevas entradas por esto."
            )

        # -- Modo Scalping Intradia (ADITIVO, ver config.ScalpingConfig) -------
        # DECISION DE ARQUITECTURA (leer el comentario largo junto a
        # ScalpingConfig en config.py antes de tocar esto): a pedido
        # EXPLICITO del usuario (2026-09-03, "Modo nuevo aparte, octubre
        # sigue como esta"), este modo NO reemplaza a self.strategy/
        # self.active_strategy_name de arriba - corre SIEMPRE DESPUES,
        # como un modulo bolt-on completamente independiente, gateado por
        # su PROPIO flag (SETTINGS.scalping.enabled /
        # GGAL_BOT_ENABLE_SCALPING, default False). Con el flag apagado
        # (default), nada de este bloque tiene ningun efecto: la posicion
        # de Octubre bajo weekly_asymmetric sigue gestionada exactamente
        # igual que antes de este modulo, linea por linea.
        self.scalping_enabled = SETTINGS.scalping.enabled
        # GGAL_BOT_ACTIVE_STRATEGY=scalping (seleccion EXCLUYENTE, ver
        # VALID_STRATEGIES en config.py) implica scalping SIEMPRE, sin
        # importar GGAL_BOT_ENABLE_SCALPING: bajo esta seleccion,
        # self.strategy queda en None (weekly_asymmetric/vol_arbitrage
        # apagados por completo, ver arriba), asi que si tambien el modulo
        # aditivo de scalping quedara apagado el bot no evaluaria NINGUNA
        # entrada nueva en todo el proceso - un bot "encendido" que en la
        # practica no hace nada, sin ningun aviso claro de por que.
        if self.active_strategy_name == "scalping" and not self.scalping_enabled:
            logger.warning(
                "GGAL_BOT_ACTIVE_STRATEGY=scalping pero GGAL_BOT_ENABLE_SCALPING=false: se "
                "fuerza el modulo de scalping a ENCENDIDO de todos modos - bajo esta seleccion "
                "exclusiva, weekly_asymmetric/vol_arbitrage ya estan apagados por completo, asi "
                "que sin esto el bot no operaria ninguna entrada nueva en absoluto."
            )
            self.scalping_enabled = True
        self.scalping_strategy: Optional[ScalpingStrategy] = None
        self.scalping_position_sizer: Optional[PositionSizer] = None
        self.scalping_risk_manager: Optional[RiskManager] = None
        self.intraday_engine: Optional[MultiTimeframeIntradayEngine] = None
        self._scalping_last_ta_snapshot_logged = None
        self._scalping_max_positions_logged = False
        if self.scalping_enabled:
            # CORRECCION (2026-09-03, ver comentario largo junto a
            # ScalpingConfig.max_vega_total/max_gamma_total en config.py y
            # README "Interaccion con el techo de Griegas"): scalping tiene
            # su PROPIO RiskManager/RiskLimits, NO el self.risk_manager
            # compartido con weekly_asymmetric/vol_arbitrage. Antes de esta
            # correccion, un book de weekly_asymmetric que ya excedia el
            # techo de vega/gamma (evaluado sobre self.portfolio.
            # total_greeks(), es decir la cuenta ENTERA) bloqueaba tambien
            # cualquier entrada nueva de scalping via should_halt_new_
            # positions(), aunque scalping no tuviera ninguna posicion
            # propia abierta - se confirmo en produccion (vega=9550.13 >
            # RiskConfig.max_vega_total=5000.0 bloqueando entradas de
            # ambas estrategias por igual). Con este RiskManager separado,
            # evaluado solo contra las Griegas de posiciones con
            # strategy_tag="scalping" (ver _greeks_for_strategy() /
            # _act_on_entry_signal(risk_manager=...) mas abajo), cada
            # estrategia queda sujeta unicamente a su propio techo, igual
            # que ya ocurria con el capital.
            self.scalping_risk_manager = RiskManager(RiskLimits(
                max_vega_total=SETTINGS.scalping.max_vega_total,
                max_gamma_total=SETTINGS.scalping.max_gamma_total,
                # El piso de liquidez de punta (spread/tamaño de libro/
                # volumen diario) SI se comparte: mide calidad de mercado
                # de la cotizacion en si, no presupuesto de cartera de una
                # estrategia particular - no hay razon para que scalping
                # tolere una punta de peor calidad que weekly_asymmetric.
                max_spread_relative=SETTINGS.risk.max_spread_relative,
                min_book_size=SETTINGS.risk.min_book_size,
                min_daily_volume=SETTINGS.risk.min_daily_volume,
            ))
            self.scalping_strategy = ScalpingStrategy(self.scalping_risk_manager, config=SETTINGS.scalping)
            # Sizer/capital PROPIOS y SEPARADOS del de weekly_asymmetric/
            # vol_arbitrage (ver PositionSizer, que ya soporta overrides por
            # instancia) - ver ScalpingConfig.max_capital_ars/
            # max_risk_pct_per_trade/max_concurrent_positions.
            self.scalping_position_sizer = PositionSizer(
                max_capital_ars=SETTINGS.scalping.max_capital_ars,
                max_risk_pct_per_trade=SETTINGS.scalping.max_risk_pct_per_trade,
                min_contracts=SETTINGS.scalping.min_contracts_per_trade,
            )
            self.intraday_engine = MultiTimeframeIntradayEngine(config=SETTINGS.scalping)
            if self.active_strategy_name == "scalping":
                logger.warning(
                    "Modo SCALPING intradia ACTIVADO como estrategia EXCLUYENTE "
                    "(GGAL_BOT_ACTIVE_STRATEGY=scalping): weekly_asymmetric/vol_arbitrage estan "
                    "COMPLETAMENTE apagados (ni entradas ni salidas). Opera sus propias posiciones "
                    "(Position.strategy_tag='scalping'), capital asignado "
                    "(GGAL_BOT_SCALPING_MAX_CAPITAL_ARS=$ %.2f, hasta %d posiciones concurrentes) y "
                    "sus propias reglas de entrada/salida (horizonte en minutos, cierre EOD %s, "
                    "reversion de IV). Cualquier posicion de weekly_asymmetric/vol_arbitrage que "
                    "haya quedado abierta de antes NO tiene ninguna gestion mientras esta seleccion "
                    "siga vigente - ver GgalOptionsBot._warn_orphaned_positions_for_active_strategy().",
                    SETTINGS.scalping.max_capital_ars, SETTINGS.scalping.max_concurrent_positions,
                    SETTINGS.scalping.eod_close_time if SETTINGS.scalping.eod_close_enabled else "DESACTIVADO",
                )
            else:
                logger.warning(
                    "Modo SCALPING intradia ACTIVADO (GGAL_BOT_ENABLE_SCALPING=true), como modulo "
                    "ADITIVO junto a la estrategia principal '%s': opera sus PROPIAS posiciones "
                    "(Position.strategy_tag='scalping'), con su propio capital asignado "
                    "(GGAL_BOT_SCALPING_MAX_CAPITAL_ARS=$ %.2f, hasta %d posiciones concurrentes) y sus "
                    "propias reglas de entrada/salida (horizonte en minutos, cierre EOD %s, reversion de "
                    "IV) - NO modifica ni gestiona ninguna posicion de weekly_asymmetric/vol_arbitrage.",
                    self.active_strategy_name, SETTINGS.scalping.max_capital_ars,
                    SETTINGS.scalping.max_concurrent_positions,
                    SETTINGS.scalping.eod_close_time if SETTINGS.scalping.eod_close_enabled else "DESACTIVADO",
                )

        # -- Ejecucion -----------------------------------------------------
        self.mm_engine = MarketMakingEngine(
            tick_size=SETTINGS.execution.tick_size,
            liquid_spread_relative_threshold=SETTINGS.execution.liquid_spread_relative_threshold,
        )
        self.order_gateway = order_gateway if order_gateway is not None else OrderGateway()
        self.mid_price_exec = MidPriceExecutionEngine(self.order_gateway, self.mm_engine)

        # -- Persistencia de estado ------------------------------------------
        self.state_writer = StateWriter()

        # -- Position Lifecycle Event Journal (Fase 5.3, ver
        # ggal_bot/portfolio/event_journal.py) --------------------------------
        self.position_event_journal = PositionEventJournal()

        # -- Snapshot de mercado (MEJORA 2026-09-28, ver
        # ggal_bot/data/market_snapshot_log.py) - corre SIEMPRE, sin importar
        # la estrategia activa (mismo criterio que position_event_journal
        # arriba y _warn_positions_without_valid_quote): es la base para
        # poder backtestear offline cualquier mejora futura.
        self.market_snapshot_log = MarketSnapshotLogger()

        # -- Cotizaciones de bonos para CCL implicito (MEJORA 2026-09-30,
        # ver ggal_bot/data/ccl_bond_quote_log.py y REPORT.md) - instanciado
        # SIEMPRE (igual que market_snapshot_log arriba), pero el poll HTTP
        # real solo corre si SETTINGS.shadow.enable_ccl_bond_quote_log esta
        # prendido (ver _run_cycle mas abajo) - apagado por defecto.
        self.ccl_bond_quote_log = CclBondQuoteLogger()

        # -- Embudo de señales (MEJORA 2026-09-29, ver
        # ggal_bot/data/signal_funnel_log.py y REPORT.md §12.3/§12.5 punto 5) -
        # instanciado SIEMPRE (igual que market_snapshot_log arriba), pero solo
        # escribe filas cuando LongFirstConfig/ScalpingConfig.
        # enable_signal_funnel_log esta prendido (candidate_funnel viene vacio
        # si no, ver weekly_asymmetric.py::scan_entry_signals) - costo cero
        # con el flag apagado (default).
        self.signal_funnel_log = SignalFunnelLogger()

        # -- Kill switch centralizado (Fase 5.3, ver ggal_bot/risk/kill_switch.py) --
        self.kill_switch = KillSwitch()

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
            # BRECHA VERIFICADA (mega-prompt "OPTIMIZACION EJECUTABLE",
            # seccion sobre Execution Engine/calidad de ejecucion): en
            # execution/order_gateway.py, OrderGateway.send() en modo
            # shadow fija `fill_price = state.reference_price or
            # request.price` - es decir, CADA fill simulado se ejecuta
            # exactamente al precio mid/de referencia del momento de la
            # decision, con slippage y spread simulados = 0. Esto es
            # independiente de MidPriceExecutionEngine (que si modela
            # cancelacion por movimiento adverso y mejora de precio por
            # timeout antes de decidir SI enviar la orden) - el problema es
            # que, una vez decidido enviar, el FILL en si no paga spread
            # ni slippage. Consecuencia: todo PnL/EV historico calculado
            # sobre logs/shadow_trades.csv de esta corrida es una COTA
            # SUPERIOR optimista, no una cifra neta de costos de ejecucion
            # reales. ShadowAuditLogger ya registra requested_price/
            # fill_price/reference_price por fill (ver order_gateway.py),
            # asi que la brecha entre decision y ejecucion queda disponible
            # para analisis futuro aunque hoy no se resta de ningun reporte.
            logger.warning(
                "SHADOW MODE: los fills simulados se ejecutan EXACTAMENTE al precio de "
                "referencia (mid) del momento de la decision - slippage/spread simulados = "
                "$0 (ver OrderGateway.send()). El PnL/EV de logs/shadow_trades.csv es una "
                "cota SUPERIOR optimista, NO una cifra neta de costos de ejecucion reales."
            )
            self.market_feed = LiveShadowFeed(on_book_update=self._on_book_update)
        else:
            self.market_feed = MarketDataFeed(on_book_update=self._on_book_update)
        self.ws_manager: Optional[WebSocketConnectionManager] = None
        self._subscribed_tickers: List[str] = []

        self._spot_book: Optional[OrderBookSnapshot] = None
        self._recent_volumes: Dict[str, float] = {}
        # Cooldown de reentrada post stop_loss para vol_arbitrage (MEJORA
        # 2026-09-17, ver VolArbitrageConfig.reentry_cooldown_seconds y
        # _check_vol_arbitrage_exits/_act_on_signal): symbol -> timestamp
        # (time.time()) hasta el cual NO se debe reabrir esa base tras un
        # cierre por stop_loss. Vacio = ninguna base en cooldown todavia
        # (comportamiento identico al de antes de esta mejora mientras
        # reentry_cooldown_seconds siga en None).
        self._vol_arbitrage_reentry_cooldown_until: Dict[str, float] = {}
        # Alerta activa por posicion sin cotizacion vigente (MEJORA
        # 2026-09-17, ver RiskConfig.stale_quote_warning_seconds y
        # _warn_positions_without_valid_quote): symbol -> timestamp
        # (time.time()) desde el cual esa base, con una posicion abierta,
        # dejo de tener una cotizacion `bid>0 and ask>0` en self.option_chain.
        self._position_missing_quote_since: Dict[str, float] = {}
        # Bases ya alertadas esta racha (evita repetir el mismo
        # logger.warning en cada ciclo de ~2-4s mientras la condicion
        # persista - mismo criterio que self._option_staleness_logged).
        self._position_missing_quote_warned: set = set()
        self._shutting_down = False
        # Numero de señal (SIGINT/SIGTERM) que disparo el shutdown, o None si
        # todavia no se recibio ninguna. Se guarda aca (en vez de loguearse
        # directamente desde el signal handler) por la razon documentada en
        # _install_signal_handlers().
        self._shutdown_signal: Optional[int] = None

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
        # Idem, para el gate de horario de rueda (MEJORA 2026-10-01, ver
        # RiskConfig.enforce_market_hours_gate / recompute_cycle()).
        self._market_hours_gate_logged = False

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

    def _active_market_data_source_name(self) -> str:
        """
        Nombre de la fuente de datos activa este ciclo - "real" (broker/
        pyRofex de verdad, modo no-shadow) o, en modo shadow, el nombre de
        la clase de LiveShadowFeed._source (ej. "MockReplaySource",
        "Data912RestSource"). Usado por _market_data_is_reliable_for_trading()
        y en el log del gate de horario (MEJORA 2026-10-01).
        """
        if not self.shadow_mode:
            return "real"
        return self.market_feed.active_source_name

    def _market_data_is_reliable_for_trading(self) -> bool:
        """
        Gate de horario de rueda (MEJORA 2026-10-01, URGENTE a pedido
        explicito del usuario - ver RiskConfig.enforce_market_hours_gate y
        ggal_bot/market_hours.py para la evidencia real completa). False si
        no se debe evaluar NINGUNA entrada/salida/hedge este ciclo porque:

          1. El flag esta desactivado explicitamente
             (RiskConfig.enforce_market_hours_gate=False) - siempre True en
             ese caso, comportamiento previo a esta mejora.
          2. Estamos fuera de la rueda asumida (ggal_bot.market_hours.
             is_within_byma_session(), 11:00-17:00 ART Lun-Vie, SUPUESTO no
             verificado - ver docstring de ese modulo).
          3. En modo shadow, la fuente activa es MockReplaySource (100%
             sintetica, sin ningun concepto de horario de rueda) - AUNQUE
             el reloj diga que es horario de rueda: si el failover
             automatico cayo a Mock (ver ShadowConfig.source_failure_
             threshold), el dato sigue siendo fabricado, no real.
        """
        if not SETTINGS.risk.enforce_market_hours_gate:
            return True
        if not market_hours.is_within_byma_session():
            return False
        if self.shadow_mode and self.market_feed.active_source_name == "MockReplaySource":
            return False
        return True

    def _is_earnings_blackout(self, today: date) -> bool:
        """
        Blackout de earnings (MEJORA 2026-09-28, ver
        config.LongFirstConfig.enable_earnings_blackout/earnings_dates/
        earnings_blackout_days_before): True si `today` cae dentro de la
        ventana [fecha_earnings - earnings_blackout_days_before,
        fecha_earnings] para alguna fecha configurada. Con
        `earnings_dates` vacio (default, ver _env_date_list) esto es
        siempre False, sin importar el valor de enable_earnings_blackout -
        no hay ninguna fecha inventada contra la cual bloquear.
        """
        cfg = SETTINGS.long_first
        if not getattr(cfg, "enable_earnings_blackout", False):
            return False
        for earnings_date in getattr(cfg, "earnings_dates", ()):
            days_before = (earnings_date - today).days
            if 0 <= days_before <= cfg.earnings_blackout_days_before:
                return True
        return False

    # -- Conexion y arranque -------------------------------------------------

    def _reconcile_portfolio_on_startup(self) -> None:
        """
        Fase 5.3 (ver ggal_bot/portfolio/reconciliation.py y
        AUDITORIA_FASE5.3_*.md, "ROOT CAUSE RESOLVED"): reconstruye
        self.portfolio ANTES de que el loop principal evalue ninguna señal,
        para que un restart de proceso ya no deje a Guarda 2 viendo una
        posicion "en cero" que en realidad seguia abierta. Solo corre en
        shadow mode (ver alcance explicito documentado en reconciliation.py)
        y solo si el portfolio esta vacio (nunca pisa posiciones ya creadas
        en este mismo proceso).

        ACTUALIZACION 2026-10-01 (Tarea #27/#28, a pedido explicito del
        usuario): reconstruye desde logs/position_events.csv (el Event
        Journal, via reconstruct_positions_from_event_journal) en vez de
        logs/shadow_trades.csv - ver "ACTUALIZACION 2026-10-01" en el
        docstring de reconciliation.py para el detalle completo de los dos
        bugs reales verificados contra produccion que esto corrige
        (contaminacion de Griegas/cantidad entre estrategias sobre el mismo
        simbolo, y position_id que se reinventaba en cada restart).

        Defensivo por diseño: cualquier problema aca (CSV corrupto, pandas
        no instalado, lo que sea) se loguea y el bot sigue con Portfolio()
        vacio - el comportamiento identico al de ANTES de esta fase -, en
        vez de abortar el arranque. Nunca fabrica una posicion.
        """
        if not SETTINGS.shadow.reconcile_portfolio_on_startup:
            logger.info(
                "Reconciliacion de portfolio al arranque DESACTIVADA "
                "(GGAL_BOT_SHADOW_RECONCILE_ON_STARTUP=false) - arrancando con portfolio vacio."
            )
            return
        if self.portfolio.positions:
            return
        try:
            positions, warnings = reconstruct_positions_from_event_journal(option_chain=self.option_chain)
        except ReconciliationUnavailable as exc:
            logger.warning(
                "Reconciliacion de portfolio al arranque OMITIDA (arrancando con portfolio "
                "vacio, comportamiento previo a Fase 5.3): %s", exc,
            )
            return
        except Exception:
            logger.exception(
                "Error inesperado reconciliando el portfolio al arranque - se continua con "
                "portfolio vacio para no bloquear el arranque del bot."
            )
            return

        for w in warnings:
            logger.warning("Reconciliacion de arranque: %s", w)

        if not positions:
            logger.info(
                "Reconciliacion de arranque: logs/shadow_trades.csv no dejo ninguna posicion "
                "neta abierta - portfolio arranca vacio (esperado si el bot cerro todo antes "
                "del ultimo restart, o si es la primera corrida)."
            )
            return

        for pos in positions:
            self.portfolio.add(pos)
        logger.warning(
            "Reconciliacion de arranque: %d posicion(es) restauradas desde logs/shadow_trades.csv "
            "(%s) - ver ggal_bot/portfolio/reconciliation.py para el alcance/limitaciones exactas "
            "de esta reconstruccion.",
            len(positions), ", ".join(f"{p.symbol}={p.quantity:g}" for p in positions),
        )

    def _warn_orphaned_positions_for_active_strategy(self) -> None:
        """
        Advertencia FUERTE, agregada 2026-09-07 junto con la seleccion
        EXCLUYENTE de estrategia (ver VALID_STRATEGIES/StrategyConfig en
        config.py: GGAL_BOT_ACTIVE_STRATEGY ahora acepta "scalping" ademas
        de "weekly_asymmetric"/"vol_arbitrage", a pedido explicito del
        usuario tras ser advertido de esta misma consecuencia).

        A diferencia del modo Scalping ADITIVO original (decision
        deliberada del usuario del 2026-09-03, ver el comentario largo
        junto a ScalpingConfig, de NO hacer esto por esta misma razon), la
        seleccion excluyente apaga por completo la gestion - ni entradas
        NI SALIDAS (Stop Loss/Take Profit/horizonte/guardia de fin de
        semana) - de cualquier posicion que no pertenezca a la estrategia
        activa vigente. Esta funcion NUNCA bloquea nada (la eleccion ya fue
        tomada explicitamente) - unicamente deja constancia, por cada
        posicion huerfana con su cantidad, de que nadie la esta vigilando,
        para que nunca sea una sorpresa silenciosa como la que motivo esta
        misma fase (ver AUDITORIA_FASE5.2_LIFECYCLE_ROOT_CAUSE.md).

        Bajo "vol_arbitrage" no se declara NINGUN strategy_tag como
        gestionado: su ciclo (_run_vol_arbitrage_cycle -> strategy.
        scan_for_signals) no recibe el portfolio ni filtra por
        strategy_tag, asi que no hay ninguna garantia real de gestion que
        declarar sobre posiciones preexistentes bajo ese modo - reportarlas
        como huerfanas es lo unico honesto, no una limitacion nueva de este
        cambio (el modo vol_arbitrage no esta en uso en produccion, ver
        docs/AUDITORIA_MAESTRA_2026-08-27.md).
        """
        managed_tags = set()
        if self.active_strategy_name == "weekly_asymmetric":
            managed_tags.add("weekly_asymmetric")
        elif self.active_strategy_name == "scalping":
            managed_tags.add("scalping")
        # "vol_arbitrage": deliberadamente ningun tag (ver docstring arriba).
        if self.scalping_enabled:
            managed_tags.add("scalping")

        orphaned: Dict[str, float] = {}
        for pos in self.portfolio.positions:
            if pos.quantity == 0:
                continue
            tag = pos.strategy_tag or "weekly_asymmetric"
            if tag not in managed_tags:
                orphaned[pos.symbol] = orphaned.get(pos.symbol, 0.0) + pos.quantity

        if orphaned:
            logger.warning(
                "ATENCION - posiciones SIN NINGUNA gestion de riesgo bajo la seleccion exclusiva "
                "vigente (GGAL_BOT_ACTIVE_STRATEGY=%s, modulo de scalping %s): %s. Ni Stop Loss, "
                "ni Take Profit, ni horizonte semanal, ni guardia de fin de semana se evaluan "
                "sobre estas bases mientras esta seleccion siga vigente - permanecen abiertas "
                "hasta que las cierres a mano o reinicies el bot con la estrategia que las "
                "gestiona. Ver GgalOptionsBot._warn_orphaned_positions_for_active_strategy().",
                self.active_strategy_name,
                "activado" if self.scalping_enabled else "desactivado",
                {k: round(v, 4) for k, v in orphaned.items()},
            )

    def connect_and_subscribe(self) -> bool:
        if self.shadow_mode:
            # Sin PyRofex, sin websocket: bootstrap_universe() arma el
            # universo (real via data912.com o sintetico via Mock/Replay) y
            # subscribe() es solo informativo. El refresco de datos ocurre
            # en cada recompute_cycle() via market_feed.poll() (ver abajo).
            self._subscribed_tickers = self.market_feed.bootstrap_universe(self.option_chain)
            self.market_feed.subscribe(self._subscribed_tickers)
            self._reconcile_portfolio_on_startup()
            self._warn_orphaned_positions_for_active_strategy()
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

        # Alerta activa por posicion sin cotizacion vigente (MEJORA
        # 2026-09-17, ver RiskConfig.stale_quote_warning_seconds): corre
        # SIEMPRE, sin importar la estrategia activa (mismo criterio que
        # option_chain.recompute_all arriba) - se ubica DESPUES de
        # recompute_all (para ver el estado de la cadena ya actualizado
        # este ciclo) y ANTES del kill switch/dispatch de estrategia, para
        # que la alerta sea lo mas temprana posible dentro del ciclo.
        self._warn_positions_without_valid_quote()
        self._warn_position_invariant_violations()

        # Snapshot de mercado (MEJORA 2026-09-28, ver
        # data/market_snapshot_log.py): corre SIEMPRE, sin importar la
        # estrategia activa (mismo criterio que la alerta de arriba) - se
        # ubica DESPUES de recompute_all (cadena ya actualizada este ciclo)
        # para que cada fila refleje IV/griegas recien calculados, no los
        # del ciclo anterior. Un fallo de disco al loguear esto nunca debe
        # tumbar el ciclo de trading real (ver MarketSnapshotLogger._write_rows).
        self.market_snapshot_log.log_quotes(self.option_chain.all_quotes())

        # Cotizaciones de bonos para CCL implicito (MEJORA 2026-09-30, ver
        # ggal_bot/data/ccl_bond_quote_log.py) - MISMO ciclo que el snapshot
        # de mercado de arriba, a pedido explicito del usuario. Opt-in
        # (apagado por defecto): con el flag apagado esta llamada es un
        # no-op completo, sin ningun poll HTTP adicional.
        if SETTINGS.shadow.enable_ccl_bond_quote_log:
            self.ccl_bond_quote_log.fetch_and_log()

        # Kill switch centralizado (Fase 5.3, ver ggal_bot/risk/kill_switch.py):
        # se evalua ANTES de correr el escaneo de entradas de este ciclo,
        # contra el estado del portfolio tal cual quedo al final del ciclo
        # anterior, para que un breach recien detectado bloquee las
        # entradas de ESTE ciclo (no solo del siguiente). No evalua
        # max_daily_loss_ars aca (requeriria pandas/dashboard.pnl_engine,
        # ver RiskLimitsConfig.max_daily_loss_ars - ese chequeo especifico
        # queda deshabilitado hasta que un caller le pase
        # realized_pnl_today_ars explicitamente, ver TODO de Fase 5.3
        # siguiente commit). evaluate() es un no-op si ya esta disparado
        # (no lo re-dispara con una reason distinta) y NUNCA resetea solo.
        # `spot=spot` (ya calculado arriba este mismo ciclo) habilita el
        # nuevo chequeo de riesgo direccional agregado
        # (RiskLimitsConfig.max_portfolio_delta_ars, ver kill_switch.py) -
        # sin esto ese limite, aunque configurado, nunca se evaluaria.
        if not self.kill_switch.is_tripped():
            self.kill_switch.evaluate(self.portfolio, SETTINGS.risk_limits, spot=spot)

        # Gate de horario de rueda (MEJORA 2026-10-01, URGENTE - ver
        # RiskConfig.enforce_market_hours_gate y ggal_bot/market_hours.py
        # para el detalle completo y la evidencia real que lo motivo).
        # Deliberadamente UN SOLO punto de corte para entradas+salidas+hedge
        # de las 3 estrategias, en vez de tocar cada evaluate_position_exit()
        # por separado: evita entradas Y salidas basadas en un precio
        # fabricado/no vigente, sin alterar la guardia de staleness existente
        # (max_market_data_staleness_seconds), que sigue cubriendo su caso
        # original (una caida REAL y transitoria de la fuente DURANTE la
        # rueda - ver docstring de esa guardia en config.py).
        if not self._market_data_is_reliable_for_trading():
            if not self._market_hours_gate_logged:
                logger.info(
                    "Fuera de horario de rueda o fuente de datos sintetica (%s): se omite "
                    "evaluacion de entradas/salidas/hedge este ciclo (spot=%.4f). No vuelve a "
                    "logearse hasta que la condicion cambie.",
                    self._active_market_data_source_name(), spot,
                )
                self._market_hours_gate_logged = True
            all_signals: List[object] = []
        else:
            self._market_hours_gate_logged = False
            if self.active_strategy_name == "vol_arbitrage":
                all_signals = self._run_vol_arbitrage_cycle(spot)
            elif self.active_strategy_name == "scalping":
                # Seleccion EXCLUYENTE (ver VALID_STRATEGIES en config.py):
                # ni weekly_asymmetric ni vol_arbitrage corren en absoluto bajo
                # este valor - self.strategy es None (ver __init__), asi que
                # ninguna de esas dos ramas puede llamarse aca. El ciclo de
                # scalping en si se dispara mas abajo, igual que en modo
                # aditivo (self.scalping_enabled esta FORZADO a True en
                # __init__ cuando active_strategy_name=="scalping").
                all_signals = []
            else:
                all_signals = self._run_weekly_asymmetric_cycle(spot)

            # Modulo de Scalping Intradia (ver ScalpingConfig/
            # GGAL_BOT_ENABLE_SCALPING y el comentario largo en __init__): con
            # GGAL_BOT_ACTIVE_STRATEGY=scalping corre como estrategia PRINCIPAL
            # (self.scalping_enabled forzado a True en __init__, la rama de
            # arriba ya dejo all_signals=[]); en cualquier otro caso, sigue
            # siendo el modulo ADITIVO original que corre SIEMPRE DESPUES de la
            # estrategia principal, nunca en su lugar - con el flag apagado
            # (default) esta llamada es un no-op completo.
            if self.scalping_enabled:
                all_signals.extend(self._run_scalping_cycle(spot))

        totals = self.portfolio.total_greeks()
        if self.risk_manager.should_halt_new_positions(totals):
            logger.warning(self.risk_manager.breach_report(totals))

        if self._market_data_is_reliable_for_trading():
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
            env_flags=list_ggal_bot_env_vars(),
            deployed_git_sha=get_deployed_git_sha(),
            shadow_mode_enabled=SETTINGS.shadow.enabled,
        )

    def _check_vol_arbitrage_exits(self, spot: float) -> None:
        """
        MEJORA 2026-09-17 (ver VolArbitrageConfig.__doc__ en config.py para
        la evidencia completa: GFGC8000OC perdio -$681.053 en shadow tras
        13 dias sin NINGUN control de riesgo, porque este modo nunca
        evaluaba salidas). "Salidas primero" (mismo orden que
        _run_weekly_asymmetric_cycle) - se corre ANTES de escanear entradas
        nuevas en el mismo ciclo, reutilizando RiskManager.
        evaluate_position_exit() tal cual (misma fuente de verdad de
        "cuando cerrar" que el resto del bot) y _act_on_exit_signal() para
        ejecutar el cierre. Deliberadamente sin tiered stop ni toma de
        ganancia parcial (ver VolArbitrageConfig.__doc__) - el objetivo es
        que ninguna posicion quede sin ningun corte, no replicar el motor
        de salida completo de weekly_asymmetric.

        `SETTINGS.vol_arbitrage.enabled=False` restaura el comportamiento
        de siempre (esta funcion no hace nada) - backward-compatible.
        """
        cfg = SETTINGS.vol_arbitrage
        if not cfg.enabled:
            return
        now = datetime.now(timezone.utc)
        for position in self.portfolio.positions:
            if position.strategy_tag != "vol_arbitrage" or position.quantity <= 0:
                continue
            if position.entry_price is None or position.entry_time is None or position.expiry is None:
                continue
            quote = self.option_chain.get(position.symbol)
            current_price = quote.book.mid if (quote is not None and quote.book.mid > 0) else None
            reason = self.risk_manager.evaluate_position_exit(
                entry_price=position.entry_price, current_price=current_price,
                entry_time=position.entry_time, now=now, expiry=position.expiry,
                stop_loss_pct=cfg.stop_loss_pct, take_profit_pct=cfg.take_profit_pct,
                max_holding_business_days=cfg.max_holding_business_days,
                weekend_theta_guard_enabled=cfg.weekend_theta_guard_enabled,
            )
            if reason is not None:
                logger.info(
                    "Señal de salida [vol_arbitrage]: %s reason=%s (sin esta mejora, esta posicion "
                    "no se hubiera evaluado nunca - ver VolArbitrageConfig.__doc__).",
                    position.symbol, reason,
                )
                self._act_on_exit_signal(
                    ExitSignal(symbol=position.symbol, reason=reason, quantity=position.quantity),
                    spot, strategy_tag="vol_arbitrage",
                )
                # Cooldown de reentrada (MEJORA 2026-09-17, ver
                # VolArbitrageConfig.reentry_cooldown_seconds): UNICAMENTE
                # tras stop_loss - es la unica razon de cierre que refleja
                # que la tesis de esta señal salio mal (take_profit es un
                # buen resultado; horizonte/guardia de fin de semana son
                # calendario, no juicio sobre la señal). Evidencia real (ver
                # analisis del export de trades 2026-09-17T17-13, base
                # GFGC7600OC): scan_for_signals() re-emite la MISMA
                # dislocacion persistente en cada ciclo (ver docstring de
                # _act_on_signal) - sin este cooldown, en cuanto Guarda 2 se
                # libera (qty vuelve a 0 tras el cierre) el bot puede
                # reabrir la MISMA base de inmediato, en el ciclo
                # inmediatamente siguiente, contra una IV que probablemente
                # todavia no se corrigio - "encadenando" stops en vez de
                # dejar que la sonrisa se normalice. Default None (ver
                # config.py) = sin cooldown, comportamiento identico al de
                # antes de esta mejora.
                if reason == "stop_loss" and cfg.reentry_cooldown_seconds is not None:
                    self._vol_arbitrage_reentry_cooldown_until[position.symbol] = (
                        time.time() + cfg.reentry_cooldown_seconds
                    )

    def _warn_position_invariant_violations(self) -> None:
        """
        Tarea #27 item 4: escaneo PASIVO (solo alerta, no bloquea nada - los
        pre-trade guards de _act_on_exit_signal ya bloquean lo que pueden
        ANTES de que ocurra) del estado ACTUAL del portfolio contra los dos
        invariantes de ggal_bot/risk/invariants.py. Corre SIEMPRE que la
        guarda este activada (ver RiskConfig.enforce_position_invariants),
        sin importar la estrategia activa - detecta una violacion ya
        consumada (ej. remanente de un bug anterior a este fix, o una
        ventana breve entre dos pasos del mismo ciclo) incluso si ningun
        guard la bloqueo a tiempo.
        """
        if not SETTINGS.risk.enforce_position_invariants:
            return
        violations = invariants.check_portfolio_invariants(self.portfolio)
        for v in violations:
            logger.error("INVARIANTE DE RIESGO VIOLADA (estado actual del portfolio): %s", v)

    def _warn_positions_without_valid_quote(self, now: Optional[float] = None) -> None:
        """
        MEJORA 2026-09-17 (ver RiskConfig.stale_quote_warning_seconds para
        la motivacion completa). Se llama SIEMPRE, sin importar la
        estrategia activa (ver recompute_cycle, mismo criterio que
        option_chain.recompute_all/kill_switch.evaluate arriba) - una
        posicion de CUALQUIER strategy_tag (weekly_asymmetric/vol_arbitrage/
        scalping) puede quedarse sin cotizacion vigente.

        `now`: timestamp inyectable (time.time()) para que este metodo sea
        testeable de forma deterministica - default None usa el reloj real
        (unico metodo del modulo que llama time.time() internamente en vez
        de recibirlo siempre inyectado, porque a diferencia de
        strategy/weekly_asymmetric.py este archivo SI mezcla I/O con logica
        de estado en varios puntos, ver _check_vol_arbitrage_exits arriba).
        """
        threshold = SETTINGS.risk.stale_quote_warning_seconds
        if threshold is None:
            return
        now_ts = now if now is not None else time.time()

        symbols_with_open_positions = {p.symbol for p in self.portfolio.positions if p.quantity != 0}
        # Purga bases que ya no tienen posicion abierta (se cerraron este
        # ciclo o antes) - evita una fuga de memoria de simbolos viejos en
        # los dos dict/set de estado de abajo.
        for symbol in list(self._position_missing_quote_since.keys()):
            if symbol not in symbols_with_open_positions:
                del self._position_missing_quote_since[symbol]
                self._position_missing_quote_warned.discard(symbol)

        for symbol in symbols_with_open_positions:
            quote = self.option_chain.get(symbol)
            has_valid_quote = quote is not None and quote.book.bid > 0 and quote.book.ask > 0
            if has_valid_quote:
                if symbol in self._position_missing_quote_since:
                    logger.info(
                        "Cotizacion recuperada para %s (posicion abierta): se resuelve la alerta "
                        "de cotizacion faltante.", symbol,
                    )
                    del self._position_missing_quote_since[symbol]
                    self._position_missing_quote_warned.discard(symbol)
                continue

            missing_since = self._position_missing_quote_since.get(symbol)
            if missing_since is None:
                self._position_missing_quote_since[symbol] = now_ts
                continue

            elapsed = now_ts - missing_since
            if elapsed >= threshold and symbol not in self._position_missing_quote_warned:
                logger.warning(
                    "ALERTA: la posicion abierta en %s no tiene cotizacion valida (bid/ask) en "
                    "la cadena vigente hace %.0fs (umbral=%.0fs) - Stop Loss/Take Profit/toma de "
                    "ganancia parcial/compresion de vega NO se estan evaluando para esta base "
                    "mientras esto dure (ver risk.risk_manager.evaluate_position_exit); solo el "
                    "horizonte de dias habiles y la guardia de fin de semana le siguen aplicando. "
                    "Requiere revision manual (ver dashboard, pestaña 'Abiertas', o "
                    "ggal_bot/ops/manual_close.py).",
                    symbol, elapsed, threshold,
                )
                self._position_missing_quote_warned.add(symbol)

    def _run_vol_arbitrage_cycle(self, spot: float) -> List[object]:
        """Ciclo bajo el modo original de arbitraje de volatilidad delta-neutral."""
        self._check_vol_arbitrage_exits(spot)
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

    def _log_entry_scan_diagnostics_if_due(
        self,
        diagnostics_by_expiry: Dict[object, EntryScanDiagnostics],
        now: datetime,
        quote_availability_by_expiry: Optional[Dict[object, Dict[str, int]]] = None,
    ) -> None:
        """
        Loguea (throttleado, ver __init__:
        _entry_diagnostics_log_interval_seconds) un resumen de por que no
        se generaron señales de entrada este ciclo: cuantas cotizaciones se
        descartaron en cada filtro, y la candidata MAS CERCANA a calificar
        (cuanto le falto en puntos de vol al umbral de dislocacion
        vigente). Puro logging, no cambia ningun umbral ni comportamiento -
        ver docstring de EntryScanDiagnostics para el porque se agrego.

        AMPLIACION (2026-09-01, mismo pedido - se detecto que el log de
        arriba nunca estaba apareciendo en produccion): scan_entry_signals()
        solo se llama por vencimiento si `len(valid_quotes) >= 3` (ver
        _run_weekly_asymmetric_cycle, filtro de `q.iv is not None and not
        q.book.is_stale(...)` ANTES del loop de EntryScanDiagnostics) - si
        NINGUN vencimiento llega a ese piso, `diagnostics_by_expiry` queda
        vacio y el log de arriba nunca se dispara, sin dejar ninguna pista
        de por que. `quote_availability_by_expiry` (total/validas/stale/
        sin IV por vencimiento) hace visible ESE cuello de botella anterior
        - tipicamente opciones sin punta vigente (bid=ask=0, "sin punta",
        ver BrokerRestSource._parse_quote_record) que nunca calculan IV, no
        necesariamente stale por conectividad.
        """
        if not diagnostics_by_expiry and not quote_availability_by_expiry:
            return
        now_ts = now.timestamp() if hasattr(now, "timestamp") else time.time()
        last_logged = self._last_entry_diagnostics_logged_at
        if last_logged is not None and (now_ts - last_logged) < self._entry_diagnostics_log_interval_seconds:
            return
        self._last_entry_diagnostics_logged_at = now_ts

        if quote_availability_by_expiry:
            for expiry, counts in quote_availability_by_expiry.items():
                logger.info(
                    "Disponibilidad de cotizaciones [venc=%s]: %d totales, %d validas (IV "
                    "calculable y no-stale), %d sin punta vigente (IV no calculable), %d "
                    "stale (> %.0fs) - %s.",
                    expiry, counts["total"], counts["valid"], counts["no_iv"], counts["stale"],
                    SETTINGS.risk.max_option_quote_staleness_seconds,
                    "no llega al piso de 3 validas para escanear entradas este ciclo"
                    if counts["valid"] < 3 else "llega al piso de 3 validas",
                )

        if not diagnostics_by_expiry:
            return

        total = EntryScanDiagnostics(trend=next(iter(diagnostics_by_expiry.values())).trend)
        best_miss: Optional[EntryScanDiagnostics] = None
        for d in diagnostics_by_expiry.values():
            total.total_quotes += d.total_quotes
            total.blocked_by_direction += d.blocked_by_direction
            total.blocked_by_holding_days += d.blocked_by_holding_days
            total.blocked_by_min_days_to_expiry += d.blocked_by_min_days_to_expiry
            total.blocked_by_liquidity += d.blocked_by_liquidity
            total.blocked_by_obi += d.blocked_by_obi
            total.blocked_by_moneyness += d.blocked_by_moneyness
            total.blocked_by_delta_band += d.blocked_by_delta_band
            total.evaluated_for_dislocation += d.evaluated_for_dislocation
            total.blocked_by_dislocation += d.blocked_by_dislocation
            total.qualified += d.qualified
            if d.closest_miss_shortfall_vol_points is not None and (
                best_miss is None
                or d.closest_miss_shortfall_vol_points < best_miss.closest_miss_shortfall_vol_points
            ):
                best_miss = d

        logger.info(
            "Diagnostico escaneo de entradas [tendencia=%s]: %d cotizaciones evaluadas -> "
            "bloqueadas por direccion tecnica=%d, horizonte semanal=%d, piso min. vencimiento=%d, "
            "liquidez=%d, OBI=%d, moneyness=%d, banda de delta=%d; llegaron al chequeo de "
            "dislocacion de smile=%d (no alcanzaron el umbral=%d, calificaron=%d).",
            total.trend, total.total_quotes, total.blocked_by_direction, total.blocked_by_holding_days,
            total.blocked_by_min_days_to_expiry, total.blocked_by_liquidity, total.blocked_by_obi,
            total.blocked_by_moneyness, total.blocked_by_delta_band, total.evaluated_for_dislocation,
            total.blocked_by_dislocation, total.qualified,
        )
        if total.evaluated_for_dislocation == 0 and total.total_quotes > 0:
            logger.info(
                "Ninguna cotizacion llego a evaluarse contra el umbral de dislocacion de smile este "
                "ciclo: el cuello de botella esta en un filtro ANTERIOR (direccion tecnica/horizonte/"
                "liquidez/OBI/moneyness), no en el umbral de smile en si."
            )
        elif best_miss is not None:
            logger.info(
                "Candidata mas cercana a calificar: %s con dislocacion observada=%.2f vol pts "
                "(se necesitaba <= %.2f) - le faltaron %.2f vol pts bajo tendencia %s.",
                best_miss.closest_miss_symbol, best_miss.closest_miss_dislocation,
                best_miss.closest_miss_threshold_required, best_miss.closest_miss_shortfall_vol_points,
                total.trend,
            )

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
        # Vol realizada de referencia (MEJORA 2026-09-28, ver
        # config.TechnicalAnalysisConfig.enable_jump_robust_hv y
        # TechnicalAnalysisEngine.hv_estimate) - se degrada a None bajo el
        # MISMO criterio que `trend`/`momentum_shift` mas abajo (datos
        # sinteticos no pueden confirmar ni gatillar ninguna decision real).
        hv_estimate: Optional[float] = None
        if self.technical_engine is not None:
            try:
                snapshot = self.technical_engine.refresh(now=now)
                trend = snapshot.trend.value if hasattr(snapshot.trend, "value") else snapshot.trend
                # Momentum Shift / Early Reversal Override (ver
                # data/technical_analysis.py:MomentumShift): mismo
                # TechnicalSnapshot que `trend`, inyectado igual que `trend`
                # en scan_entry_signals() - ver docstring de ese metodo.
                momentum_shift = snapshot.momentum_shift
                # BUG REAL CORREGIDO (ver seguimiento de auditoria del
                # 2026-09-01, incidente en produccion: data912 tiro
                # SSLEOFError en el endpoint de velas historicas y el motor
                # cayo a SyntheticDailyBarsSource - ver
                # data/technical_analysis.py): un trend calculado sobre
                # barras 100% inventadas NO puede gatillar ni confirmar
                # entradas. Antes snapshot.data_source solo se logueaba sin
                # afectar el filtro direccional - una racha de mala suerte
                # con data912 podia hacer que el bot tomara una direccion de
                # entrada basada en un grafico ficticio. Se degrada a
                # NEUTRAL, el mismo estado conservador que ya se usa cuando
                # el refresh de tendencia tira una excepcion (ver except mas
                # abajo), y se apaga el Momentum Shift Override (calculado
                # sobre las mismas barras sinteticas).
                if snapshot.data_source == "synthetic":
                    trend = Trend.NEUTRAL.value
                    momentum_shift = None
                else:
                    hv_estimate = self.technical_engine.hv_estimate()
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
                    if snapshot.data_source == "synthetic":
                        logger.warning(
                            "Tendencia 1D calculada sobre datos SINTETICOS (data912 no disponible o "
                            "insuficiente) - se fuerza a NEUTRAL este ciclo y NO se usa para gatillar "
                            "ni confirmar entradas ni completar spreads."
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
        exit_signals = self.strategy.build_exit_signals(
            self.portfolio, current_prices, now, current_greeks=current_greeks, trend=trend,
        )
        all_signals.extend(exit_signals)
        for ex in exit_signals:
            logger.info("Salida [Long-First]: %s %s x%.2f - %s", ex.action, ex.symbol, ex.quantity, ex.reason)
            self._act_on_exit_signal(ex, spot)

        # -- 1b) Patas cortas de spread descubiertas (Tarea #27 item 3) ---------
        # Corre INMEDIATAMENTE despues de las salidas de arriba, en el MISMO
        # ciclo: si una de esas salidas (o cualquier otra anterior) dejo una
        # pata corta sin su larga, se recompra aca mismo - ver
        # WeeklyAsymmetricStrategy.build_naked_short_wing_exit_signals.
        naked_wing_signals = self.strategy.build_naked_short_wing_exit_signals(self.portfolio)
        all_signals.extend(naked_wing_signals)
        for nw in naked_wing_signals:
            logger.warning(
                "Salida [Long-First]: %s %s x%.2f - %s", nw.action, nw.symbol, nw.quantity, nw.reason,
            )
            self._act_on_naked_short_wing_exit_signal(nw, spot)

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
        entry_diagnostics_by_expiry: Dict[object, object] = {}
        quote_availability_by_expiry: Dict[object, Dict[str, int]] = {}
        # Blackout de earnings (MEJORA 2026-09-28, ver
        # config.LongFirstConfig.enable_earnings_blackout): un solo chequeo
        # por ciclo, reusado para todos los vencimientos de abajo.
        earnings_blackout = self._is_earnings_blackout(now.date())
        if not market_data_stale:
            for expiry, quotes in self.option_chain.quotes_by_expiry().items():
                # Vencimiento forzado (a pedido explicito del usuario,
                # 2026-09-01 - ver InstrumentsConfig.forced_expiry, validado
                # y logueado una sola vez en __init__): se ignora POR
                # COMPLETO cualquier otro vencimiento, ni siquiera se
                # calculan valid_quotes/diagnosticos para el - el usuario
                # eligio explicitamente operar solo este.
                if self._forced_expiry is not None and expiry != self._forced_expiry:
                    continue
                # Ver comentario equivalente en _run_vol_arbitrage_cycle:
                # una opcion excluida del recalculo por staleness (option_chain.
                # recompute_all()) conserva su ultimo IV conocido, que ya no es
                # comparable contra el resto de la sonrisa recalculada con el
                # spot actual - se excluye de la deteccion de señales de entrada.
                is_stale_threshold = SETTINGS.risk.max_option_quote_staleness_seconds
                valid_quotes = [
                    q for q in quotes
                    if q.iv is not None and not q.book.is_stale(is_stale_threshold)
                ]
                # Diagnostico puro (ver _log_entry_scan_diagnostics_if_due): cuenta
                # POR QUE una cotizacion no llego a "valida" - sin punta vigente
                # (iv no calculable) vs. stale por conectividad - para no confundir
                # ambos motivos cuando `valid_quotes` queda corto.
                quote_availability_by_expiry[expiry] = {
                    "total": len(quotes),
                    "valid": len(valid_quotes),
                    "no_iv": sum(1 for q in quotes if q.iv is None),
                    "stale": sum(1 for q in quotes if q.book.is_stale(is_stale_threshold)),
                }
                if len(valid_quotes) < 3:
                    continue
                surface = VolatilitySurface(valid_quotes)

                # z-score de dislocacion (MEJORA 2026-09-28, ver
                # config.LongFirstConfig.enable_zscore_filter): se alimenta
                # el tracker con TODAS las valid_quotes de este vencimiento
                # (no solo las que terminan calificando), mismo criterio que
                # IVMeanReversionTracker en Scalping - antes de aplicar
                # ningun filtro. Con el flag apagado, se salta el trabajo
                # por completo (el parametro queda en None, identico a no
                # pasarlo).
                dislocation_zscore = None
                if SETTINGS.long_first.enable_zscore_filter:
                    dislocation_zscore = {}
                    for q in valid_quotes:
                        dislocation = surface.smile_dislocation(q)
                        self._dislocation_tracker.update(q.symbol, dislocation, now=now)
                        dislocation_zscore[q.symbol] = self._dislocation_tracker.zscore(q.symbol)

                entry_signals = self.strategy.scan_entry_signals(
                    surface, self._recent_volumes, hv_estimate=hv_estimate,
                    trend=trend, momentum_shift=momentum_shift,
                    dislocation_zscore=dislocation_zscore, earnings_blackout=earnings_blackout,
                    now=now,
                )
                if self.strategy.last_scan_diagnostics is not None:
                    entry_diagnostics_by_expiry[expiry] = self.strategy.last_scan_diagnostics
                    self.signal_funnel_log.log_funnel(
                        "weekly_asymmetric", self.strategy.last_scan_diagnostics.candidate_funnel, now=now,
                    )
                all_signals.extend(entry_signals)
                for es in entry_signals:
                    logger.info(
                        "Señal [%s]: %s %s (%.2f vol pts, score conv.=%.4f) - %s",
                        expiry, es.action, es.symbol, es.iv_dislocation_vol_points, es.convexity_score, es.reason,
                    )
                    self._act_on_entry_signal(es, spot)

                # Spread de debito como ENTRADA NUEVA cuando la IV esta cara
                # (MEJORA 2026-09-17, ver config.LongFirstConfig.
                # enable_expensive_iv_spread_entry, apagado por defecto).
                # Va DENTRO de este loop (no despues, como el paso 3 de
                # completar spreads) porque necesita la `surface` de ESTE
                # vencimiento puntual, igual que scan_entry_signals arriba.
                #
                # DELIBERADAMENTE SOLO SEÑALIZA/LOGUEA todavia, NO ejecuta
                # ordenes: a diferencia de scan_spread_completion_signals
                # (que solo agrega una pata corta a una larga YA confirmada,
                # un solo envio de orden), esto requeriria coordinar DOS
                # ordenes nuevas (comprar la larga + vender el wing) como
                # una unidad, con su propio sizing contra el debito neto (no
                # la prima simple) - superficie de ejecucion nueva que el
                # usuario todavia no confirmo que quiere activa en modo
                # shadow real. Ver WeeklyAsymmetricStrategy.
                # scan_expensive_iv_spread_signals para la logica de
                # deteccion, ya cubierta por tests.
                if SETTINGS.long_first.enable_expensive_iv_spread_entry:
                    expensive_spread_signals = self.strategy.scan_expensive_iv_spread_signals(
                        surface, self.option_chain, self._recent_volumes, trend=trend,
                        max_quote_age_seconds=SETTINGS.risk.max_option_quote_staleness_seconds,
                        now=time.time(),
                    )
                    for sp in expensive_spread_signals:
                        logger.info(
                            "Señal [%s, spread de debito, SOLO LOG - ejecucion no implementada "
                            "todavia]: comprar %s / vender %s (debito neto=%.2f) - %s",
                            expiry, sp.long_symbol, sp.short_symbol, sp.net_debit_premium, sp.reason,
                        )

            self._log_entry_scan_diagnostics_if_due(
                entry_diagnostics_by_expiry, now, quote_availability_by_expiry=quote_availability_by_expiry,
            )

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
                    now=time.time(), forced_expiry=self._forced_expiry,
                )
                all_signals.extend(spread_signals)
                for sp in spread_signals:
                    logger.info(
                        "Spread [Long-First]: %s sobre %s (cubre %s) - %s",
                        sp.action, sp.short_symbol, sp.long_symbol, sp.reason,
                    )
                    self._act_on_spread_completion_signal(sp, spot)

            # -- 4) Spread de debito como ENTRADA NUEVA cuando la IV esta cara
            # (MEJORA 2026-09-17, ver config.LongFirstConfig.
            # enable_expensive_iv_spread_entry, apagado por defecto).
            #
            # DELIBERADAMENTE SOLO SEÑALIZA/LOGUEA todavia, NO ejecuta ordenes:
            # a diferencia de scan_spread_completion_signals (que solo agrega
            # una pata corta a una larga YA confirmada, un solo envio de
            # orden), esto requeriria coordinar DOS ordenes nuevas (comprar la
            # larga + vender el wing) como una unidad, con su propio sizing
            # contra el debito neto (no la prima simple) - superficie de
            # ejecucion nueva que el usuario todavia no confirmo que quiere
            # activa en modo shadow real. Ver WeeklyAsymmetricStrategy.
            # scan_expensive_iv_spread_signals para la logica de deteccion,
            # ya cubierta por tests.
            if SETTINGS.long_first.enable_expensive_iv_spread_entry:
                expensive_spread_signals = self.strategy.scan_expensive_iv_spread_signals(
                    surface, self.option_chain, self._recent_volumes, trend=trend,
                    max_quote_age_seconds=SETTINGS.risk.max_option_quote_staleness_seconds,
                    now=time.time(),
                )
                for sp in expensive_spread_signals:
                    logger.info(
                        "Señal [spread de debito, SOLO LOG - ejecucion no implementada todavia]: "
                        "comprar %s / vender %s (debito neto=%.2f) - %s",
                        sp.long_symbol, sp.short_symbol, sp.net_debit_premium, sp.reason,
                    )

        return all_signals

    def _run_scalping_cycle(self, spot: float) -> List[object]:
        """
        Ciclo del modulo ADITIVO de Scalping Intradia (ver ScalpingConfig/
        GGAL_BOT_ENABLE_SCALPING) - se llama SIEMPRE DESPUES del ciclo de
        la estrategia principal (ver recompute_cycle()), nunca en su lugar.
        Estructura de DOS pasos (deliberadamente SIN paso de spreads: este
        modo opera unicamente Long Call/Long Put desnudas de alta rotacion,
        ver docstring de strategy/scalping.py:ScalpingStrategy):

            0) Tendencia intradia MULTI-TIMEFRAME (5m/15m por defecto, ver
               data/intraday_bars.py:MultiTimeframeIntradayEngine): se
               alimenta con el spot de ESTE MISMO ciclo (misma fuente que
               ya usa el resto del bot, no una fuente de datos nueva) y se
               refresca con su propio cache corto (ScalpingConfig.
               refresh_interval_seconds, tipicamente 30s).
            1) SALIDAS primero (ScalpingStrategy.build_exit_signals): Stop
               Loss/Take Profit ajustados, horizonte de holding en MINUTOS,
               cierre obligatorio de Fin de Dia (EOD) y salida por
               reversion de la dislocacion de IV que motivo la entrada.
            2) ENTRADAS nuevas, respetando el tope de posiciones
               concurrentes (ScalpingConfig.max_concurrent_positions) para
               repartir el capital asignado en mas trades de menor tamaño.

        Todas las posiciones que abre este ciclo quedan marcadas
        `Position.strategy_tag="scalping"` (ver portfolio/portfolio.py) -
        esa marca es la que mantiene esta gestion completamente aislada de
        weekly_asymmetric/vol_arbitrage (ver ScalpingStrategy.
        build_exit_signals y WeeklyAsymmetricStrategy.build_exit_signals,
        que solo procesan las posiciones marcadas con su propio tag).
        """
        assert self.scalping_strategy is not None and self.intraday_engine is not None
        now = datetime.now(timezone.utc)
        all_signals: List[object] = []

        # -- 0) Tendencia intradia multi-timeframe -------------------------------
        self.intraday_engine.on_tick(now, spot)
        snapshot = self.intraday_engine.refresh(now=now)
        trend = snapshot.combined_trend
        if snapshot is not self._scalping_last_ta_snapshot_logged:
            logger.info(
                "Tendencia intradia SCALPING (%dm/%dm, %s): %s [5m=%s (%s barras), 15m=%s (%s barras)]",
                SETTINGS.scalping.fast_bar_interval_minutes, SETTINGS.scalping.slow_bar_interval_minutes,
                "requiere acuerdo" if snapshot.require_agreement else "solo timeframe rapido",
                trend, snapshot.fast.trend.value, snapshot.fast.bars_used,
                snapshot.slow.trend.value, snapshot.slow.bars_used,
            )
            self._scalping_last_ta_snapshot_logged = snapshot

        # -- 1) Salidas primero ---------------------------------------------------
        current_prices = {
            q.symbol: q.book.mid for q in self.option_chain.all_quotes()
            if q.book.bid > 0 and q.book.ask > 0
        }
        exit_signals = self.scalping_strategy.build_exit_signals(self.portfolio, current_prices, now)
        all_signals.extend(exit_signals)
        for ex in exit_signals:
            logger.info("Salida [Scalping]: %s %s x%.2f - %s", ex.action, ex.symbol, ex.quantity, ex.reason)
            self._act_on_exit_signal(ex, spot, strategy_tag="scalping")

        # -- Guardia de staleness de datos de mercado (mismo criterio que -------
        # weekly_asymmetric - ver RiskConfig.max_market_data_staleness_seconds):
        # las salidas de arriba ya se procesaron con la ultima punta conocida,
        # pero no se abre exposicion nueva contra un spot desactualizado.
        if self._is_market_data_stale(now):
            return all_signals

        # -- Tope de posiciones concurrentes --------------------------------------
        open_scalping_positions = sum(
            1 for p in self.portfolio.positions
            if p.quantity > 0 and p.greeks_per_unit is not None and p.strategy_tag == "scalping"
        )
        if open_scalping_positions >= SETTINGS.scalping.max_concurrent_positions:
            if not self._scalping_max_positions_logged:
                logger.info(
                    "Scalping: tope de posiciones concurrentes alcanzado (%d/%d) - no se evaluan "
                    "entradas nuevas hasta que se libere un cupo (por una salida).",
                    open_scalping_positions, SETTINGS.scalping.max_concurrent_positions,
                )
                self._scalping_max_positions_logged = True
            return all_signals
        self._scalping_max_positions_logged = False

        # -- 2) Entradas nuevas ----------------------------------------------------
        for expiry, quotes in self.option_chain.quotes_by_expiry().items():
            if open_scalping_positions >= SETTINGS.scalping.max_concurrent_positions:
                break
            is_stale_threshold = SETTINGS.risk.max_option_quote_staleness_seconds
            valid_quotes = [q for q in quotes if q.iv is not None and not q.book.is_stale(is_stale_threshold)]
            if len(valid_quotes) < 3:
                continue
            surface = VolatilitySurface(valid_quotes)
            order_books = {q.symbol: q.book for q in valid_quotes}
            entry_signals = self.scalping_strategy.scan_entry_signals(
                surface, self._recent_volumes, order_books, trend=trend, now=now,
            )
            if self.scalping_strategy.last_scan_diagnostics is not None:
                self.signal_funnel_log.log_funnel(
                    "scalping", self.scalping_strategy.last_scan_diagnostics.candidate_funnel, now=now,
                )
            for es in entry_signals:
                if open_scalping_positions >= SETTINGS.scalping.max_concurrent_positions:
                    break
                logger.info(
                    "Señal [Scalping %s]: %s %s (%.2f vol pts, score conv.=%.4f) - %s",
                    expiry, es.action, es.symbol, es.iv_dislocation_vol_points, es.convexity_score, es.reason,
                )
                before_qty = self._position_quantity(es.symbol)
                self._act_on_entry_signal(
                    es, spot, strategy_tag="scalping", position_sizer=self.scalping_position_sizer,
                    risk_manager=self.scalping_risk_manager,
                )
                if self._position_quantity(es.symbol) != before_qty:
                    open_scalping_positions += 1
                all_signals.append(es)

        return all_signals

    def _position_quantity(self, symbol: str) -> float:
        """Posicion neta (signed) actualmente registrada en self.portfolio para `symbol`."""
        return sum(p.quantity for p in self.portfolio.positions if p.symbol == symbol)

    def _symbol_is_tradeable_now(self, symbol: str) -> bool:
        """
        True si `symbol` tiene una cotizacion operable AHORA MISMO (bid/ask
        validos) y no hay ya una orden en vigilancia sobre esa base - mismo
        criterio exacto que _act_on_exit_signal/_act_on_naked_short_wing_exit_signal
        usan antes de intentar ejecutar. Inyectado en
        ggal_bot.risk.invariants.naked_short_wing_violation (Tarea #27 item
        4) para decidir si una pata corta se puede recubrir ESTE ciclo.
        """
        quote = self.option_chain.get(symbol)
        if quote is None or quote.book.bid <= 0 or quote.book.ask <= 0:
            return False
        return not self.mid_price_exec.has_open_order_for(symbol)

    def _log_guard2(
        self, *, caller: str, symbol: str, strategy_tag: str, existing_quantity: float,
        reason: str, blocked: bool,
    ) -> None:
        """
        Instrumentacion de Guarda 2 (TANDA 2 "OPTIMIZACION EJECUTABLE",
        seccion 5, 2026-09-08): antes de este cambio, el rechazo de Guarda 2
        se logueaba con `logger.debug(...)` en los tres call sites
        (_act_on_entry_signal/_act_on_signal/_act_on_spread_completion_signal)
        - si el nivel de log en produccion es INFO (lo tipico), esas lineas
        NUNCA quedan escritas, asi que un "ADD" inesperado en logs no se
        podia correlacionar con si Guarda 2 lo bloqueo o lo dejo pasar. Esta
        funcion centraliza el log a nivel INFO (siempre visible) para AMBOS
        resultados (bloqueado y permitido), con exactamente los campos
        pedidos para poder reproducir un caso real: estrategia, simbolo,
        cantidad existente ANTES de esta señal, motivo de la señal, quien
        llamo (que guard-2 especifico, de las tres rutas de entrada
        existentes), resultado del guard y timestamp (via el propio logger).
        No incluye order/client_order_id aca: a esta altura del codigo
        (ANTES de intentar el fill) todavia no existe una orden - ver
        el "ENTRY"/"REJECT" en self.position_event_journal (mas abajo en
        cada metodo) para la correlacion con esos IDs una vez que el fill
        se intenta.
        """
        logger.info(
            "Guarda 2 [%s]: symbol=%s strategy_tag=%s existing_quantity=%.4f reason=%s -> %s",
            caller, symbol, strategy_tag, existing_quantity, reason,
            "BLOQUEADO (ya existe posicion)" if blocked else "OK (sin posicion previa)",
        )

    def _capital_available_ars(self, strategy_tag: str = "weekly_asymmetric") -> float:
        """
        Capital libre para nuevas entradas de la estrategia `strategy_tag`:
        el techo configurado para ESA estrategia (`LongFirstConfig.
        max_capital_ars` para "weekly_asymmetric", `ScalpingConfig.
        max_capital_ars` para "scalping" - ver portfolio.Position.
        strategy_tag y el modulo ADITIVO de Scalping en config.ScalpingConfig)
        menos lo ya comprometido en posiciones LARGAS DE OPCIONES abiertas
        CON ESA MISMA MARCA, valuado a su propio precio de ENTRADA (no a
        mercado - ver risk/position_sizer.py, que dimensiona contra capital
        comprometido, no contra PnL flotante). Nunca negativo.

        Cada estrategia tiene su PROPIO pool de capital, completamente
        separado: una posicion de scalping nunca reduce el capital
        disponible para weekly_asymmetric y viceversa - una posicion sin
        marca (`strategy_tag is None`, toda posicion abierta antes de que
        este parametro existiera, incluida la de Octubre en produccion)
        cuenta como "weekly_asymmetric", preservando el comportamiento
        exacto de antes de este parametro para el llamador que no lo pasa.

        Se excluye explicitamente la posicion del subyacente que deja el
        delta-hedger (ver _maybe_hedge(): `greeks_per_unit is None` es la
        marca de "esto es el subyacente, no una opcion" - ver
        portfolio.Position). El presupuesto de capital de cada modo es para
        comprar CONVEXIDAD (opciones), no para la cobertura de delta, que es
        una decision de riesgo separada (global, sobre el delta TOTAL de la
        cuenta) y no deberia competir por el mismo presupuesto ni reducir el
        sizing de la proxima señal de entrada de ninguna estrategia.
        """
        cfg = SETTINGS.long_first if strategy_tag == "weekly_asymmetric" else SETTINGS.scalping
        committed = sum(
            pos.quantity * (pos.entry_price or 0.0) * pos.multiplier
            for pos in self.portfolio.positions
            if pos.quantity > 0 and pos.entry_price is not None and pos.greeks_per_unit is not None
            and (pos.strategy_tag or "weekly_asymmetric") == strategy_tag
        )
        return max(0.0, cfg.max_capital_ars - committed)

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
        ciclos siguientes. Sin las guardas de abajo, el bot reentraba la
        MISMA base una y otra vez (una orden nueva cada ciclo, sin límite),
        porque nunca quedaba registro de que ya se había operado esa señal.
        Las guardas son deliberadamente simples (una base por vez, sin
        pyramideo) - no implementan una logica de salida/take-profit propia
        de esta señal (esa la aporta _check_vol_arbitrage_exits, MEJORA
        2026-09-17, ver VolArbitrageConfig.__doc__: antes de esa mejora,
        "cerrar la posicion cuando la sonrisa se normalice" era un TODO sin
        resolver).

        Guarda 0 (MEJORA 2026-09-17, ver VolArbitrageConfig.
        reentry_cooldown_seconds y _check_vol_arbitrage_exits): con la
        salida automatica por stop_loss ya activa, la MISMA señal
        persistente de arriba puede volver a calificar en el ciclo
        siguiente al cierre - sin este cooldown, el bot reabriria la base
        recien stopeada de inmediato, encadenando stops en vez de esperar a
        que la sonrisa se normalice de verdad (ver analisis real de
        GFGC7600OC en el export de trades 2026-09-17T17-13). Default
        (reentry_cooldown_seconds=None) preserva el comportamiento previo a
        esta mejora (sin cooldown).
        """
        quote = self.option_chain.get(signal.symbol)
        if quote is None or quote.book.bid <= 0 or quote.book.ask <= 0:
            return

        # Guarda 0: base todavia en cooldown de reentrada tras un stop_loss
        # reciente (ver docstring de arriba y VolArbitrageConfig.
        # reentry_cooldown_seconds).
        cooldown_cfg = SETTINGS.vol_arbitrage.reentry_cooldown_seconds
        if cooldown_cfg is not None:
            cooldown_until = self._vol_arbitrage_reentry_cooldown_until.get(signal.symbol)
            if cooldown_until is not None and time.time() < cooldown_until:
                logger.debug(
                    "Señal %s ignorada: base en cooldown de reentrada tras stop_loss (faltan %.0fs).",
                    signal.symbol, cooldown_until - time.time(),
                )
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
        existing_qty = self._position_quantity(signal.symbol)
        if existing_qty != 0:
            self._log_guard2(
                caller="_act_on_signal(vol_arbitrage)", symbol=signal.symbol, strategy_tag="vol_arbitrage",
                existing_quantity=existing_qty, reason=getattr(signal, "reason", ""), blocked=True,
            )
            return
        self._log_guard2(
            caller="_act_on_signal(vol_arbitrage)", symbol=signal.symbol, strategy_tag="vol_arbitrage",
            existing_quantity=existing_qty, reason=getattr(signal, "reason", ""), blocked=False,
        )

        # AISLAMIENTO por strategy_tag (ver Portfolio.greeks_for_strategy_tag
        # y el mismo fix aplicado a _act_on_entry_signal mas abajo, 2026-09-
        # 03): las posiciones de este metodo nunca llevan strategy_tag
        # explicito (quedan en None, ver Position.add() mas abajo), que por
        # convencion se trata como "weekly_asymmetric" en todo el resto del
        # bot (ver Position.strategy_tag) - se evalua consistente con eso,
        # para que un book de scalping corriendo en paralelo (bolt-on, ver
        # ScalpingConfig) no bloquee entradas de vol_arbitrage ni viceversa.
        totals = self.portfolio.greeks_for_strategy_tag("weekly_asymmetric")
        if self.risk_manager.should_halt_new_positions(totals):
            logger.info(
                "Señal %s descartada: la cartera de '%s' ya excede sus limites de riesgo (Griegas: %s).",
                signal.symbol, self.active_strategy_name, totals,
            )
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
            new_pos = Position(
                symbol=signal.symbol, quantity=signed_qty,
                multiplier=SETTINGS.instruments.option_multiplier,
                greeks_per_unit=quote.greeks, expiry=quote.expiry,
                # entry_price/entry_time: metadata para el modo Long-First
                # (ver risk.risk_manager.RiskManager.evaluate_position_exit
                # y strategy/weekly_asymmetric.py) - se pobla aca sin
                # importar que estrategia este activa, porque sin esto
                # ningun Stop Loss/Take Profit/horizonte semanal es evaluable.
                entry_price=state.avg_fill_price, entry_time=datetime.now(timezone.utc),
                # FIX 2026-09-17 (ver VolArbitrageConfig en config.py): antes
                # esto quedaba en None, que "por convencion" el resto del
                # bot trata como "weekly_asymmetric" (ver comentario en
                # Position.strategy_tag) - una posicion de este modo podia
                # terminar invisible para SU PROPIA gestion de riesgo y a la
                # vez adoptable por otra estrategia sin que nadie lo
                # decidiera. Tag explicito: aisla sus Griegas/capital/
                # salidas igual que ya se hace con "scalping".
                strategy_tag="vol_arbitrage",
            )
            self.portfolio.add(new_pos)
            new_pos.contract_key = (
                f"{SETTINGS.instruments.underlying_symbol}|{new_pos.symbol}|{quote.expiry.isoformat()}"
                if quote.expiry is not None else None
            )
            # MEJORA 2026-09-30 (Prioridad 2, a pedido explicito del
            # usuario: "que vol_arbitrage tambien escriba eventos en
            # position_events.csv con su strategy_tag - fuente unica de
            # verdad sin excepciones"): antes de esto, la salida de una
            # posicion de vol_arbitrage SI quedaba logueada (via
            # _act_on_exit_signal/_check_vol_arbitrage_exits, que ya pasa
            # strategy_tag=pos.strategy_tag), pero su ENTRY nunca se
            # loggeaba - una asimetria real (verificada leyendo el codigo:
            # este metodo solo hacia self.portfolio.add(...), sin ningun
            # log_event) que dejaba a match_trades_fifo() sin Position ID
            # para resolver estos fills (ver dashboard/pnl_engine.py,
            # resolve_position_ids_from_journal) y al panel de
            # reconciliacion sin cobertura de Chequeo 2 para esta
            # estrategia. Mismo patron exacto que _act_on_entry_signal.
            self.position_event_journal.log_event(
                "ENTRY", position_id=new_pos.position_id, contract_key=new_pos.contract_key,
                symbol=new_pos.symbol, strategy_tag="vol_arbitrage",
                side="buy" if side is OrderSide.BUY else "sell",
                quantity_delta=new_pos.quantity, quantity_after=new_pos.quantity,
                price=new_pos.entry_price,
                order_client_id=getattr(getattr(state, "request", None), "client_order_id", ""),
                reason=getattr(signal, "reason", ""),
                data_unavailable_fields=() if new_pos.contract_key else ("contract_key",),
            )

    def _act_on_exit_signal(self, signal, spot: float, strategy_tag: str = "weekly_asymmetric") -> None:
        """
        Cierra (sell_to_close) la posicion larga que disparo la señal de
        salida (ver risk.risk_manager.RiskManager.evaluate_position_exit /
        evaluate_scalping_exit, unica fuente de verdad de "cuando cerrar"
        bajo cada modo). `strategy_tag`: solo se vacian posiciones marcadas
        con este tag (ver portfolio.Position.strategy_tag) - defensivo, ya
        que en la practica solo existe UN lote abierto por simbolo en todo
        el bot (ver Guarda 2 de _act_on_entry_signal), de una sola
        estrategia a la vez.

        Igual que _act_on_signal()/_act_on_entry_signal() (modo
        vol_arbitrage y weekly_asymmetric/scalping respectivamente), este
        metodo asume a lo sumo un lote abierto por base (sin pyramideo - la
        Guarda 2 de _act_on_entry_signal() es la que sostiene esa
        invariante): por eso, tras el fill, alcanza con vaciar a 0 todas
        las posiciones largas marcadas de ese simbolo, sin necesitar
        trackear que lote especifico genero la señal.

        EXCEPCION (MEJORA 2026-09-04, ver risk_manager.
        evaluate_partial_profit_take): cuando `signal.reason ==
        "partial_profit_take"` la posicion NO se vacia por completo - se
        descuenta unicamente `signal.quantity` (la fraccion configurada,
        ver config.LongFirstConfig.partial_profit_take_fraction) y se marca
        `Position.partial_profit_taken = True` para que esta salida no
        vuelva a dispararse sobre el "runner" restante. Cualquier otro
        motivo de cierre (Stop Loss/Take Profit/horizonte/guardia de fin de
        semana/compresion de vega, y las salidas de scalping) preserva el
        comportamiento de siempre: vaciar a 0.
        """
        quote = self.option_chain.get(signal.symbol)
        if quote is None or quote.book.bid <= 0 or quote.book.ask <= 0:
            logger.warning("Salida %s no ejecutable este ciclo: sin punta operable.", signal.symbol)
            return

        if self.mid_price_exec.has_open_order_for(signal.symbol):
            logger.debug("Salida %s pospuesta: ya hay una orden en vigilancia sobre esa base.", signal.symbol)
            return

        # Invariantes duros de posicion (Tarea #27 item 4, ver
        # ggal_bot/risk/invariants.py) - ultima linea de defensa, nunca
        # deberian dispararse en el camino normal, pero si lo hacen,
        # BLOQUEAN la orden y alertan en vez de dejarla pasar.
        if SETTINGS.risk.enforce_position_invariants:
            confirmed_long = invariants.confirmed_long_quantity(self.portfolio, signal.symbol, strategy_tag)
            # INVARIANTE 1: solo se bloquea para partial_profit_take - ese
            # motivo SI es una cantidad exacta que nunca deberia exceder lo
            # confirmado (pedirlo seria un bug real). Un cierre TOTAL
            # (cualquier otro motivo) puede legitimamente pedir "cerrar todo"
            # con una cantidad nominal mayor a la disponible - el bucle de
            # abajo ya la recorta de forma segura lote por lote (fix de
            # over-close de Fase 5.3, nunca deja una Position negativa), asi
            # que no hay nada que bloquear ahi.
            violation = None
            if getattr(signal, "reason", "") == "partial_profit_take":
                violation = invariants.long_only_net_short_violation(
                    self.portfolio, signal.symbol, strategy_tag, signal.quantity,
                )
            # INVARIANTE 2: se evalua contra lo que REALMENTE se va a reducir
            # (recortado a lo confirmado, igual criterio que el bucle de
            # abajo), no contra `signal.quantity` crudo - evita falsos
            # positivos en un cierre total "de sobra".
            if violation is None:
                effective_reduce = min(signal.quantity, confirmed_long) if confirmed_long > 0 else 0.0
                violation = invariants.naked_short_wing_violation(
                    self.portfolio, signal.symbol, strategy_tag, effective_reduce,
                    wing_is_closable_now=self._symbol_is_tradeable_now,
                )
            if violation is not None:
                logger.error("INVARIANTE DE RIESGO VIOLADA - orden bloqueada: %s", violation)
                self.position_event_journal.log_event(
                    "REJECT", symbol=signal.symbol, strategy_tag=strategy_tag,
                    side="sell", reason=f"position_invariant_violation: {violation}",
                )
                return

        # INSTRUMENTACION DE CALIDAD DE EJECUCION (TANDA 2 "OPTIMIZACION
        # EJECUTABLE", seccion 9, 2026-09-08): a diferencia del path de
        # ENTRADA (donde scan_entry_signals ya excluye quotes stale ANTES de
        # emitir una señal, ver RiskConfig.max_option_quote_staleness_seconds
        # y risk_manager.check_liquidity), el path de SALIDA no tenia ningun
        # chequeo de antiguedad/calidad de la punta - un exit procedia igual
        # aunque el quote tuviera bid/ask "vivos" pero muy viejos (mid !=
        # precio ejecutable real). Deliberadamente NO se bloquea el exit por
        # esto (ver docstring de KillSwitch: una salida nunca debe quedar
        # atrapada esperando un dato mejor que puede no llegar) - se registra
        # el motivo (decision price = mid, bid/ask/spread relativo, edad real
        # del quote, cantidad solicitada) para que quede trazable si el fill
        # resultante se aleja del mid "on decision" ademas de exigir mas
        # visibilidad si la punta es vieja.
        book = quote.book
        quote_age = book.age_seconds()
        stale_threshold = SETTINGS.risk.max_option_quote_staleness_seconds
        log_fn = logger.warning if quote_age > stale_threshold else logger.info
        log_fn(
            "Salida %s [reason=%s]: decision_price(mid)=%.2f bid=%.2f ask=%.2f "
            "spread_rel=%.4f quote_age=%.1fs (umbral=%.0fs, %s) requested_qty=%.2f",
            signal.symbol, getattr(signal, "reason", ""), book.mid, book.bid, book.ask,
            book.spread_relative, quote_age, stale_threshold,
            "STALE - el mid puede no ser fielmente ejecutable" if quote_age > stale_threshold else "OK",
            signal.quantity,
        )

        state = self.mid_price_exec.submit(
            symbol=signal.symbol, book=quote.book, side=OrderSide.SELL, quantity=signal.quantity,
            spot_reference=spot, aggressive=False,
        )

        if state.status is OrderStatus.FILLED:
            # FASE 5.3 - fix del bug de "over-close" (ver
            # AUDITORIA_FASE5.2B_FORENSIC_REPLAY.md SS1): cuando existe MAS
            # DE UN objeto Position para el mismo symbol+strategy_tag
            # (fragmentacion - ver weekly_asymmetric.py:build_exit_signals,
            # que genera un ExitSignal POR CADA Position, no uno agregado
            # por symbol), el codigo anterior aplicaba la MISMA operacion
            # (vaciar a 0, o descontar signal.quantity) a TODOS los lotes
            # que matcheaban, en vez de distribuir/limitar la reduccion a
            # los `signal.quantity` contratos REALMENTE vendidos en este
            # fill. Eso podia vaciar o sobre-reducir posiciones que esta
            # señal en particular no representaba, un bug real de
            # integridad de estado (Categoria C), independiente de la
            # hipotesis de reinicio sin persistencia investigada para
            # Guarda 2.
            #
            # Fix: consumir exactamente `signal.quantity` contratos, en
            # orden FIFO (lote mas antiguo primero, por entry_time), across
            # los lotes que matchean symbol+strategy_tag+quantity>0. Si
            # is_partial, cada lote efectivamente tocado se marca
            # partial_profit_taken=True (igual que antes, pero ahora solo
            # en los lotes que de verdad se redujeron). Si la señal pedia
            # cerrar mas de lo que hay disponible entre todos los lotes
            # marcados, se deja constancia via warning en vez de fabricar
            # cantidad o fallar silenciosamente.
            is_partial = signal.reason == "partial_profit_take"
            remaining_to_reduce = signal.quantity
            matching = [
                pos for pos in self.portfolio.positions
                if pos.symbol == signal.symbol and pos.quantity > 0
                and (pos.strategy_tag or "weekly_asymmetric") == strategy_tag
            ]
            matching.sort(key=lambda p: p.entry_time or datetime.min.replace(tzinfo=timezone.utc))
            for pos in matching:
                if remaining_to_reduce <= 0:
                    break
                if is_partial:
                    reduce_qty = min(pos.quantity, remaining_to_reduce)
                else:
                    # Cierre total: sigue vaciando el lote completo (no solo
                    # signal.quantity) para preservar el comportamiento
                    # histórico de "cierre = flat" cuando hay un solo lote,
                    # pero ahora détiene la iteración una vez consumida la
                    # cantidad de la señal en vez de vaciar TODOS los lotes
                    # restantes sin relación con este fill.
                    reduce_qty = pos.quantity
                pos.quantity -= reduce_qty
                remaining_to_reduce -= reduce_qty
                if is_partial:
                    pos.partial_profit_taken = True

                # Event journal (Fase 5.3, ver ggal_bot/portfolio/event_journal.py):
                # un evento por LOTE efectivamente tocado (no uno por señal),
                # exactamente lo que corrige el bug de over-close - cada fila
                # aca representa una reduccion real de UN position_id puntual.
                self.position_event_journal.log_event(
                    "CLOSE" if pos.quantity <= 1e-9 else ("PARTIAL_EXIT" if is_partial else "REDUCE"),
                    position_id=pos.position_id, contract_key=pos.contract_key,
                    symbol=pos.symbol, strategy_tag=pos.strategy_tag or "weekly_asymmetric",
                    side="sell", quantity_delta=-reduce_qty, quantity_after=pos.quantity,
                    price=state.avg_fill_price,
                    order_client_id=getattr(getattr(state, "request", None), "client_order_id", ""),
                    reason=signal.reason,
                )
            if is_partial and remaining_to_reduce > 1e-9:
                logger.warning(
                    "Salida parcial %s: la señal pedia reducir %.2f contratos pero solo %.2f "
                    "estaban disponibles en posiciones marcadas '%s' - posible fragmentacion o "
                    "inconsistencia de estado (ver AUDITORIA_FASE5.2B_FORENSIC_REPLAY.md SS1).",
                    signal.symbol, signal.quantity, signal.quantity - remaining_to_reduce, strategy_tag,
                )

    def _act_on_naked_short_wing_exit_signal(
        self, signal, spot: float, strategy_tag: str = "weekly_asymmetric",
    ) -> None:
        """
        Ejecuta un ExitSignal `action="buy_to_close"` producido por
        WeeklyAsymmetricStrategy.build_naked_short_wing_exit_signals() -
        Tarea #27 item 3. Analogo a _act_on_exit_signal pero en la direccion
        opuesta: COMPRA `signal.quantity` contratos para recubrir una pata
        corta que quedo descubierta, reduciendo (nunca vaciando de mas) los
        lotes negativos que matcheen symbol+strategy_tag, mas antiguo
        primero - mismo criterio FIFO-por-lote que el fix de over-close de
        Fase 5.3 aplico al lado largo.
        """
        quote = self.option_chain.get(signal.symbol)
        if quote is None or quote.book.bid <= 0 or quote.book.ask <= 0:
            logger.warning(
                "Recompra de pata corta descubierta %s no ejecutable este ciclo: sin punta operable.",
                signal.symbol,
            )
            return
        if self.mid_price_exec.has_open_order_for(signal.symbol):
            logger.debug(
                "Recompra de pata corta descubierta %s pospuesta: ya hay una orden en vigilancia.",
                signal.symbol,
            )
            return

        logger.warning(
            "Pata corta descubierta detectada: %s x%.2f (razon=%s) - recomprando para evitar "
            "exposicion sin cobertura (ver Tarea #27 item 3, GFGV5000OC).",
            signal.symbol, signal.quantity, signal.reason,
        )

        state = self.mid_price_exec.submit(
            symbol=signal.symbol, book=quote.book, side=OrderSide.BUY, quantity=signal.quantity,
            spot_reference=spot, aggressive=False,
        )

        if state.status is OrderStatus.FILLED:
            remaining_to_reduce = signal.quantity
            matching = [
                pos for pos in self.portfolio.positions
                if pos.symbol == signal.symbol and pos.quantity < 0
                and (pos.strategy_tag or "weekly_asymmetric") == strategy_tag
            ]
            matching.sort(key=lambda p: p.entry_time or datetime.min.replace(tzinfo=timezone.utc))
            for pos in matching:
                if remaining_to_reduce <= 0:
                    break
                reduce_qty = min(abs(pos.quantity), remaining_to_reduce)
                pos.quantity += reduce_qty  # se acerca a 0 desde negativo
                remaining_to_reduce -= reduce_qty

                self.position_event_journal.log_event(
                    "CLOSE" if abs(pos.quantity) <= 1e-9 else "REDUCE",
                    position_id=pos.position_id, contract_key=pos.contract_key,
                    symbol=pos.symbol, strategy_tag=pos.strategy_tag or "weekly_asymmetric",
                    side="buy", quantity_delta=reduce_qty, quantity_after=pos.quantity,
                    price=state.avg_fill_price,
                    order_client_id=getattr(getattr(state, "request", None), "client_order_id", ""),
                    reason=signal.reason,
                )
            if remaining_to_reduce > 1e-9:
                logger.warning(
                    "Recompra de pata corta %s: se compraron %.2f contratos pero solo %.2f "
                    "estaban disponibles en patas cortas marcadas '%s' - revisar estado.",
                    signal.symbol, signal.quantity, signal.quantity - remaining_to_reduce, strategy_tag,
                )

    def _act_on_entry_signal(
        self, signal, spot: float, strategy_tag: str = "weekly_asymmetric",
        position_sizer: Optional[PositionSizer] = None,
        risk_manager: Optional[RiskManager] = None,
    ) -> None:
        """
        Compra (buy_to_open) la EntrySignal (de WeeklyAsymmetricStrategy o
        de ScalpingStrategy - ambas producen el mismo dataclass EntrySignal,
        ver strategy/weekly_asymmetric.py), dimensionando la cantidad de
        contratos via risk/position_sizer.py contra el capital disponible
        de `strategy_tag` (ver _capital_available_ars()) en vez de un
        tamaño fijo. Mismas guardas anti-reentrada que _act_on_signal (modo
        vol_arbitrage): no duplicar sobre una orden en vigilancia ni sobre
        una posicion ya abierta en esa base.

        `position_sizer`: instancia a usar (ver run_bot.py.__init__:
        self.position_sizer para weekly_asymmetric, self.
        scalping_position_sizer para scalping - cada estrategia tiene el
        suyo, con su propio capital/riesgo por trade). Si se omite, cae a
        `self.position_sizer` (comportamiento identico al de antes de este
        parametro, para cualquier llamador que no lo pase).

        `risk_manager`: instancia contra la que se evalua el techo de
        Griegas de ESTA entrada (ver run_bot.py.__init__: self.risk_manager
        para weekly_asymmetric/vol_arbitrage, self.scalping_risk_manager
        para scalping - CORRECCION 2026-09-03, ver comentario largo junto a
        ScalpingConfig.max_vega_total en config.py: antes de este
        parametro, todas las estrategias compartian el mismo RiskManager y
        se evaluaban contra self.portfolio.total_greeks(), es decir la
        EXPOSICION TOTAL de la cuenta - un book de una estrategia que ya
        excedia el techo bloqueaba tambien las entradas de la otra, aunque
        esta ultima no tuviera ninguna posicion propia abierta. Si se
        omite, cae a `self.risk_manager` (comportamiento identico al de
        antes de este parametro, para cualquier llamador que no lo pase).
        Se evalua siempre contra las Griegas de SOLO `strategy_tag` (ver
        Portfolio.greeks_for_strategy_tag), nunca contra la cuenta entera,
        para que el aislamiento sea efectivo incluso si dos estrategias
        terminaran compartiendo el mismo RiskManager.
        """
        quote = self.option_chain.get(signal.symbol)
        if quote is None or quote.book.bid <= 0 or quote.book.ask <= 0:
            return

        # Kill switch centralizado (Fase 5.3, ver ggal_bot/risk/kill_switch.py):
        # bloquea SOLO entradas nuevas, nunca salidas (ver docstring de
        # KillSwitch sobre por que _act_on_exit_signal no lo consulta).
        if self.kill_switch.is_tripped():
            state_ks = self.kill_switch.status()
            logger.warning(
                "Señal %s ignorada: kill switch disparado (%s: %s). Requiere reset manual "
                "('python -m ggal_bot.risk.kill_switch --reset \"motivo\"').",
                signal.symbol, state_ks.tripped_by, state_ks.reason,
            )
            self.position_event_journal.log_event(
                "REJECT", symbol=signal.symbol, strategy_tag=strategy_tag,
                side="buy", reason=f"kill_switch_tripped: {state_ks.reason}",
            )
            return

        # Guarda 1: orden de esta base ya en vigilancia.
        if self.mid_price_exec.has_open_order_for(signal.symbol):
            logger.debug("Señal %s ignorada: ya hay una orden en vigilancia sobre esa base.", signal.symbol)
            return

        # Guarda 2: ya existe una posicion abierta sobre esta base (sin
        # pyramideo) - GLOBAL a todo el bot, no solo a `strategy_tag`: dos
        # estrategias distintas nunca deben terminar con posiciones
        # simultaneas sobre la MISMA base.
        existing_qty = self._position_quantity(signal.symbol)
        if existing_qty != 0:
            self._log_guard2(
                caller="_act_on_entry_signal", symbol=signal.symbol, strategy_tag=strategy_tag,
                existing_quantity=existing_qty, reason=getattr(signal, "reason", ""), blocked=True,
            )
            return
        self._log_guard2(
            caller="_act_on_entry_signal", symbol=signal.symbol, strategy_tag=strategy_tag,
            existing_quantity=existing_qty, reason=getattr(signal, "reason", ""), blocked=False,
        )

        rm = risk_manager if risk_manager is not None else self.risk_manager
        totals = self.portfolio.greeks_for_strategy_tag(strategy_tag)
        if rm.should_halt_new_positions(totals):
            logger.info(
                "Señal %s descartada: la cartera de '%s' ya excede sus limites de riesgo (Griegas: %s).",
                signal.symbol, strategy_tag, totals,
            )
            self.position_event_journal.log_event(
                "REJECT", symbol=signal.symbol, strategy_tag=strategy_tag,
                side="buy", reason=f"greeks_limit_exceeded: {totals}",
            )
            return

        sizer = position_sizer if position_sizer is not None else self.position_sizer
        # Sizing por CONVICCION (MEJORA 2026-09-28, ver
        # risk/position_sizer.py::PositionSizer.conviction_multiplier_for):
        # 1.0 (no-op) si el flag esta apagado o `signal` no trae
        # iv_dislocation_vol_points (getattr defensivo - ScalpingStrategy
        # tambien produce EntrySignal, mismo dataclass, asi que siempre lo
        # trae hoy, pero no se asume para una señal futura distinta).
        conviction_multiplier = sizer.conviction_multiplier_for(
            getattr(signal, "iv_dislocation_vol_points", None),
        )
        sizing = sizer.compute_contracts(
            premium_price=signal.premium_reference,
            capital_available_ars=self._capital_available_ars(strategy_tag),
            conviction_multiplier=conviction_multiplier,
        )
        if not sizing.is_tradeable:
            logger.info("Señal %s descartada por sizing (%s).", signal.symbol, sizing.rejected_reason)
            self.position_event_journal.log_event(
                "REJECT", symbol=signal.symbol, strategy_tag=strategy_tag,
                side="buy", reason=f"sizing_not_tradeable: {sizing.rejected_reason}",
            )
            return

        # Presupuesto PREVENTIVO de Griegas (MEJORA 2026-09-28, ver
        # config.RiskConfig.enable_preemptive_greeks_budget y
        # risk/risk_manager.py::RiskManager.projected_greeks_breach): a
        # diferencia del chequeo de arriba (totales YA vigentes ANTES de
        # esta entrada), esto proyecta los totales CON la cantidad real ya
        # sizeada (sizing.contracts) sumada - se ubica DESPUES del sizing a
        # proposito, porque recien aca se conoce la cantidad real.
        if SETTINGS.risk.enable_preemptive_greeks_budget and quote.greeks is not None:
            added_greeks = {k: v * sizing.contracts for k, v in quote.greeks.items()}
            breach = rm.projected_greeks_breach(
                totals, added_greeks, SETTINGS.risk.preemptive_greeks_budget_fraction,
            )
            if breach is not None:
                logger.info("Señal %s descartada por presupuesto preventivo de Griegas: %s", signal.symbol, breach)
                self.position_event_journal.log_event(
                    "REJECT", symbol=signal.symbol, strategy_tag=strategy_tag,
                    side="buy", reason=f"greeks_budget_preemptive: {breach}",
                )
                return

        state = self.mid_price_exec.submit(
            symbol=signal.symbol, book=quote.book, side=OrderSide.BUY, quantity=sizing.contracts,
            spot_reference=spot, aggressive=False,
        )

        if state.status is OrderStatus.FILLED and quote.greeks is not None:
            # option_type/trend_at_entry (MEJORA 2026-09-17, ver
            # config.LongFirstConfig.enable_trend_reversal_exit): se leen de
            # la propia EntrySignal (poblados por
            # WeeklyAsymmetricStrategy.scan_entry_signals/ScalpingStrategy
            # equivalente) - getattr() defensivo porque signal puede ser
            # cualquier dataclass de señal de entrada existente que no
            # tenga estos campos (ninguno de los actuales carece de ellos,
            # pero evita un AttributeError duro si se agrega uno nuevo).
            signal_option_type = getattr(signal, "option_type", None)
            new_pos = Position(
                symbol=signal.symbol, quantity=sizing.contracts,
                multiplier=SETTINGS.instruments.option_multiplier,
                greeks_per_unit=quote.greeks, expiry=quote.expiry,
                entry_price=state.avg_fill_price, entry_time=datetime.now(timezone.utc),
                strategy_tag=strategy_tag,
                option_type=getattr(signal_option_type, "value", signal_option_type),
                trend_at_entry=getattr(signal, "trend_context", None) or None,
            )
            self.portfolio.add(new_pos)
            new_pos.contract_key = (
                f"{SETTINGS.instruments.underlying_symbol}|{new_pos.symbol}|{quote.expiry.isoformat()}"
                if quote.expiry is not None else None
            )
            self.position_event_journal.log_event(
                "ENTRY", position_id=new_pos.position_id, contract_key=new_pos.contract_key,
                symbol=new_pos.symbol, strategy_tag=strategy_tag, side="buy",
                quantity_delta=new_pos.quantity, quantity_after=new_pos.quantity,
                price=new_pos.entry_price,
                order_client_id=getattr(getattr(state, "request", None), "client_order_id", ""),
                reason=signal.reason,
                data_unavailable_fields=() if new_pos.contract_key else ("contract_key",),
            )

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
        existing_qty = self._position_quantity(signal.short_symbol)
        if existing_qty != 0:
            self._log_guard2(
                caller="_act_on_spread_completion_signal", symbol=signal.short_symbol,
                strategy_tag="weekly_asymmetric", existing_quantity=existing_qty,
                reason="spread_completion", blocked=True,
            )
            return
        self._log_guard2(
            caller="_act_on_spread_completion_signal", symbol=signal.short_symbol,
            strategy_tag="weekly_asymmetric", existing_quantity=existing_qty,
            reason="spread_completion", blocked=False,
        )

        quantity = signal.long_quantity_confirmed
        state = self.mid_price_exec.submit(
            symbol=signal.short_symbol, book=quote.book, side=OrderSide.SELL, quantity=quantity,
            spot_reference=spot, aggressive=False,
        )

        if state.status is OrderStatus.FILLED and quote.greeks is not None:
            new_short_leg = Position(
                symbol=signal.short_symbol, quantity=-quantity,
                multiplier=SETTINGS.instruments.option_multiplier,
                greeks_per_unit=quote.greeks, expiry=quote.expiry,
                entry_price=state.avg_fill_price, entry_time=datetime.now(timezone.utc),
                # strategy_tag queda en None aca a proposito (no se cambia
                # el comportamiento existente de esta Position) - "por
                # convencion" el resto del bot (Portfolio.strategy_tag,
                # _act_on_exit_signal) lo trata como "weekly_asymmetric".
                # financed_by_symbol (Tarea #27 item 3): vincula esta pata
                # corta con la larga que la financio, para que
                # build_naked_short_wing_exit_signals() pueda detectar si
                # queda descubierta cuando la larga se reduzca/cierre.
                financed_by_symbol=signal.long_symbol,
            )
            self.portfolio.add(new_short_leg)
            new_short_leg.contract_key = (
                f"{SETTINGS.instruments.underlying_symbol}|{new_short_leg.symbol}|{quote.expiry.isoformat()}"
                if quote.expiry is not None else None
            )
            # MEJORA 2026-09-30 (encontrado al implementar la Prioridad 2 del
            # usuario - "journal para todas las estrategias, sin excepciones":
            # esta pata corta de spread completion, igual que la entrada de
            # vol_arbitrage, solo hacia self.portfolio.add(...) sin loguear
            # al journal. strategy_tag explicito "weekly_asymmetric" aca
            # (coincide con el fallback `pos.strategy_tag or
            # "weekly_asymmetric"` que ya usa _act_on_exit_signal al cerrarla).
            self.position_event_journal.log_event(
                "ENTRY", position_id=new_short_leg.position_id, contract_key=new_short_leg.contract_key,
                symbol=new_short_leg.symbol, strategy_tag="weekly_asymmetric", side="sell",
                quantity_delta=new_short_leg.quantity, quantity_after=new_short_leg.quantity,
                price=new_short_leg.entry_price,
                order_client_id=getattr(getattr(state, "request", None), "client_order_id", ""),
                reason="spread_completion",
                data_unavailable_fields=() if new_short_leg.contract_key else ("contract_key",),
            )

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

        A pedido explicito del usuario (2026-09-01, ver RiskConfig.
        enable_delta_hedge / GGAL_BOT_ENABLE_DELTA_HEDGE): si el delta-hedging
        esta desactivado por config, este metodo no hace absolutamente nada -
        ni siquiera evalua needs_hedge() - el bot opera solo opciones.
        """
        if not SETTINGS.risk.enable_delta_hedge:
            return
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
            # FIX DE FRAGMENTACION (TANDA 2 "OPTIMIZACION EJECUTABLE", seccion
            # 5, 2026-09-08): ANTES de este fix, cada hedge fill exitoso
            # agregaba un objeto Position NUEVO para el subyacente (via
            # self.portfolio.add(...) incondicional) en vez de consolidar en
            # la posicion de hedge YA abierta - exactamente el mismo patron
            # de fragmentacion "multiples Position por symbol" que
            # ggal_bot/portfolio/reconciliation.py ya corrigio para el path
            # de RECONCILIACION AL ARRANQUE, pero que seguia sin corregirse
            # aca, en el path de hedge EN VIVO. Con GGAL_BOT_ENABLE_DELTA_HEDGE
            # en su default actual (True, ver RiskConfig.enable_delta_hedge -
            # ADVERTENCIA: el comentario de ese campo describe la intencion
            # del usuario de apagarlo, pero el DEFAULT DE CODIGO sigue en
            # True; verificar el valor real configurado en Northflank) y un
            # rehedge repetido, esto habria seguido creando una Position
            # nueva por cada fill de cobertura, inflando indefinidamente
            # `max_positions_per_symbol_strategy` (kill_switch.py) para el
            # subyacente hasta disparar el kill switch por "fragmentacion" -
            # una via de falsos trips COMPLETAMENTE DISTINTA de la ya
            # corregida en reconciliation.py, y candidata real a explicar
            # cualquier ADD inesperado sobre el subyacente visto en logs.
            #
            # Fix: buscar la Position de HEDGE ya abierta para este symbol
            # (marca: greeks_per_unit is None, ver portfolio.Position) y
            # consolidar ahi - promedio ponderado de precio de entrada si el
            # nuevo fill AMPLIA la posicion en la misma direccion (o si la
            # posicion previa estaba en 0), o simplemente ajustar la
            # cantidad (preservando el precio de entrada de lo que queda)
            # si el fill la esta reduciendo/revirtiendo. Nunca se crea una
            # segunda Position para el mismo subyacente mientras la primera
            # siga viva.
            existing_hedge = next(
                (p for p in self.portfolio.positions if p.symbol == state.request.symbol and p.greeks_per_unit is None),
                None,
            )
            # MEJORA 2026-09-30 (Prioridad 2, a pedido explicito del usuario:
            # "que las patas de delta_hedge tambien escriban eventos en
            # position_events.csv con su strategy_tag - fuente unica de
            # verdad sin excepciones"): antes de esto, ningun fill de
            # delta-hedge quedaba en el journal (verificado leyendo el
            # codigo: esta rama solo tocaba self.portfolio directamente).
            # A diferencia de una posicion de opciones (ENTRY/ADD/REDUCE/
            # PARTIAL_EXIT/CLOSE claros, disparados por señales discretas),
            # el hedge es una UNICA Position que se reajusta en cada ciclo
            # (ver el comentario de "FIX DE FRAGMENTACION" arriba) - nunca
            # hay un "signal.reason" que distinga partial_profit_take, asi
            # que el mapeo de evento es puramente por el efecto neto sobre
            # la cantidad: ENTRY (primera vez, o se reabre desde flat),
            # ADD (amplia en la misma direccion), REDUCE (angosta sin
            # llegar a 0) o CLOSE (llega exactamente a 0).
            if existing_hedge is None:
                new_hedge = Position(
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
                )
                self.portfolio.add(new_hedge)
                self.position_event_journal.log_event(
                    "ENTRY", position_id=new_hedge.position_id, contract_key=new_hedge.contract_key,
                    symbol=new_hedge.symbol, strategy_tag="delta_hedge",
                    side="buy" if signed_qty > 0 else "sell",
                    quantity_delta=signed_qty, quantity_after=new_hedge.quantity,
                    price=new_hedge.entry_price,
                    order_client_id=getattr(getattr(state, "request", None), "client_order_id", ""),
                    reason="delta_hedge_rebalance",
                    data_unavailable_fields=() if new_hedge.contract_key else ("contract_key",),
                )
            else:
                old_qty = existing_hedge.quantity
                same_direction_or_flat = old_qty == 0 or (old_qty > 0) == (signed_qty > 0)
                if same_direction_or_flat:
                    total_abs_qty = abs(old_qty) + abs(signed_qty)
                    if total_abs_qty > 0:
                        existing_hedge.entry_price = (
                            abs(old_qty) * (existing_hedge.entry_price or 0.0)
                            + abs(signed_qty) * state.avg_fill_price
                        ) / total_abs_qty
                # direccion contraria (reduce/revierte la cobertura previa):
                # el entry_price del remanente se preserva tal cual (no se
                # fabrica un costo nuevo para la porcion que se esta
                # cerrando), igual criterio que _act_on_exit_signal.
                existing_hedge.quantity = old_qty + signed_qty
                if existing_hedge.entry_time is None:
                    existing_hedge.entry_time = datetime.now(timezone.utc)

                if same_direction_or_flat:
                    hedge_event_type = "ENTRY" if old_qty == 0 else "ADD"
                else:
                    hedge_event_type = "CLOSE" if abs(existing_hedge.quantity) <= 1e-9 else "REDUCE"
                self.position_event_journal.log_event(
                    hedge_event_type, position_id=existing_hedge.position_id,
                    contract_key=existing_hedge.contract_key,
                    symbol=existing_hedge.symbol, strategy_tag="delta_hedge",
                    side="buy" if signed_qty > 0 else "sell",
                    quantity_delta=signed_qty, quantity_after=existing_hedge.quantity,
                    price=state.avg_fill_price,
                    order_client_id=getattr(getattr(state, "request", None), "client_order_id", ""),
                    reason="delta_hedge_rebalance",
                    data_unavailable_fields=() if existing_hedge.contract_key else ("contract_key",),
                )

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
            # Fallback defensivo: en la practica SIGINT ya es interceptado por
            # _handler() (mas abajo), que marca self._shutting_down y hace que
            # el while de arriba termine solo, sin necesidad de esta excepcion.
            pass
        finally:
            # El log de "señal recibida" se emite aca, fuera del signal
            # handler, ver la razon en _install_signal_handlers(). En este
            # punto ya estamos de vuelta en el flujo secuencial normal del
            # hilo principal (no interrumpiendo nada), asi que loguear aca es
            # seguro.
            self._log_shutdown_signal_if_any()
            self.shutdown()

    def _log_shutdown_signal_if_any(self) -> None:
        """Loguea la señal de apagado recibida (SIGINT/SIGTERM), si hubo
        alguna. Se llama SIEMPRE desde el flujo normal de ejecucion (nunca
        desde el signal handler en si), ver la razon en
        _install_signal_handlers()."""
        if self._shutdown_signal is not None:
            logger.info(
                "Señal de apagado recibida (%s); iniciando graceful shutdown.",
                self._shutdown_signal,
            )

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):  # noqa: ARG001
            # IMPORTANTE: un signal handler en Python se ejecuta en el hilo
            # principal, "insertado" en el punto exacto de bytecode en el que
            # la señal llego -- incluso si ese punto esta en medio de un
            # logger.info(...) ya en curso escribiendo al mismo stream de
            # logs. Si este handler tambien llama a logger.info(...), termina
            # reentrando el mismo _io.BufferedWriter todavia bloqueado por la
            # escritura interrumpida, lo cual dispara "RuntimeError: reentrant
            # call inside <_io.BufferedWriter ...>" (observado en produccion
            # durante un shutdown). La logica de logging del modulo `logging`
            # hace I/O, y hacer I/O dentro de un signal handler no es seguro
            # en general por esta misma razon.
            #
            # Por eso este handler NO loguea nada: solo guarda el numero de
            # señal y levanta la bandera de apagado. El mensaje se loguea de
            # forma segura despues, desde run_forever(), una vez que el hilo
            # principal volvio a su flujo de ejecucion normal (ver el
            # `finally` de run_forever()).
            self._shutdown_signal = signum
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
                env_flags=list_ggal_bot_env_vars(),
                deployed_git_sha=get_deployed_git_sha(),
                shadow_mode_enabled=SETTINGS.shadow.enabled,
            )
        except Exception:
            logger.exception("Error escribiendo el estado final durante el shutdown.")

        logger.info("Shutdown completo.")


if __name__ == "__main__":
    bot = GgalOptionsBot()
    bot.run_forever()
