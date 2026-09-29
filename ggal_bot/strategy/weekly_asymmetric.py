"""
weekly_asymmetric.py
=======================
Estrategia "Long-First / Weekly Asymmetric": modo operativo alternativo al
arbitraje de volatilidad delta-neutral original (strategy/vol_arbitrage.py,
que sigue disponible sin cambios), pensado para un ALYC que NO permite
venta en descubierto (Short Selling) de calls ni puts, con horizonte de
tenencia maximo semanal (5 ruedas habiles) y sizing dinamico por capital
asignado (ver config.LongFirstConfig y risk/position_sizer.py).

Reglas de entrada (UNICA direccion permitida: BUY to Open):
    1. Solo bases dentro del horizonte semanal configurado
       (dias habiles al vencimiento <= LongFirstConfig.max_holding_business_days).
    2. Solo bases "baratas": IV cruda por DEBAJO de la curva suavizada del
       smile en al menos smile_threshold_vol_points. Nunca se genera una
       señal para abrir sobre una base "cara" (eso seria vender para
       abrir, exactamente lo que este modo prohibe).
    3. Solo bases dentro de la banda de moneyness configurada (ATM/OTM
       cercana, donde gamma/vega por unidad de prima son mas altos: "alta
       convexidad"), rankeadas por un score de convexidad por peso de
       prima (mayor score primero).
    4. (Opcional, off por defecto) Confirmacion de nivel: el IV promedio
       del vencimiento tambien por debajo de la HV de referencia (misma
       logica que strategy/vol_arbitrage.py - ver
       VolatilitySurface.level_dislocation), para separar "barata por
       ruido de smile" de "barata en serio".
    5. Filtro direccional tecnico OBLIGATORIO (ver data/technical_analysis.py
       y config.TechnicalAnalysisConfig), inyectado como el parametro
       `trend` en scan_entry_signals()/scan_spread_completion_signals() -
       NUNCA calculado internamente aca, para mantener este modulo libre de
       I/O y testeable con datos sinteticos (mismo criterio que
       risk.risk_manager.RiskManager.evaluate_position_exit() recibe `now`
       en vez de llamar datetime.now()):
           BULLISH -> solo se consideran CALLs (Long Call / Bull Call Spread).
           BEARISH -> solo se consideran PUTs (Long Put / Bear Put Spread).
           NEUTRAL -> "cash/espera": no se completan spreads de ninguna
               familia, y una entrada nueva solo se admite si la
               dislocacion de smile es EXTREMA (smile_threshold_vol_points
               multiplicado por neutral_extreme_smile_multiplier) - un
               NEUTRAL no bloquea absolutamente todo, pero exige mucho mas
               que el umbral normal para justificar tomar exposicion sin
               una lectura tecnica direccional que la respalde.
       Un ADX/MACD/EMA "BULLISH" es una lectura de la ESTRUCTURA reciente
       de precios de GGAL, no una prediccion: filtra direccion, no
       garantiza resultado.

       Momentum Shift / Early Reversal Override (ver
       data/technical_analysis.py:MomentumShift,
       config.TechnicalAnalysisConfig.enable_momentum_shift_override):
       el filtro BULLISH/BEARISH de arriba es, por construccion, un filtro
       de ESTRUCTURA ya confirmada (EMA20/EMA50 recien cruzan varias ruedas
       despues de que el nuevo regimen arranco) - siempre llega tarde a un
       cambio de tendencia. Para no perder movimientos por esa demora sin
       eliminar la disciplina de tendencia, cuando el RSI(14) ya giro con
       fuerza EN CONTRA de la tendencia vigente (`momentum_shift` inyectado
       junto con `trend`, mismo TechnicalSnapshot), el option_type contrario
       deja de descartarse de plano: se lo vuelve a evaluar, pero exigiendo
       el mismo umbral EXTREMO de dislocacion de smile que ya rige bajo
       NEUTRAL (nunca el umbral normal) - se sigue exigiendo una dislocacion
       fuerte para operar en contra de la tendencia diaria, ahora con un
       gatillo adicional (momentum) en vez de depender solo de esperar a que
       la tendencia diaria termine de girar. Aplica unicamente a
       scan_entry_signals(): scan_spread_completion_signals() se mantiene
       estrictamente alineado a `trend`, sin excepcion por momentum (el
       reclamo que motivo este mecanismo fue especificamente sobre entradas
       tardias, no sobre el armado de spreads).
    6. Confirmacion de microestructura (ver models/microstructure.py,
       Order Book Imbalance): filtro de CALIDAD DE EJECUCION, no de alpha
       direccional - descarta una base si el libro muestra un desbalance
       extremo hacia el lado vendedor (`cfg.min_obi_for_entry`), tipico de
       una punta aislada/iliquida en un libro tan delgado como el de
       opciones de GGAL, mas que informacion genuina de precio.

Salida adicional por compresion de vega (ver
risk.risk_manager.RiskManager.evaluate_vega_decay_exit, cfg.vega_decay_exit_ratio):
complementa (no reemplaza) Stop Loss/Take Profit/horizonte semanal/guardia
de fin de semana - si el |vega| actual de una posicion ya cayo por debajo
de un porcentaje configurable del |vega| que tenia al momento de la
entrada, la tesis de convexidad que motivo la compra ya se agoto (la opcion
dejo de ser sensible a la vol) y se cierra, aunque el PnL% de la prima
todavia no dispare ninguna de las reglas anteriores.

Reglas de armado de spreads (Bull Call Spread / Bear Put Spread):
    La pata corta de un spread SOLO se contempla si el portafolio YA
    muestra una posicion LARGA CONFIRMADA en la base correspondiente (ver
    scan_spread_completion_signals) - nunca se arma ni se envia una pata
    corta de forma independiente. Esto hace de la restriccion "comprar
    primero" una invariante DE CODIGO, no solo de intencion: sin una
    Position de cantidad > 0 en el portafolio para esa base especifica, no
    existe ninguna ruta en este modulo que genere una señal de venta sobre
    esa base.

Las salidas (Stop Loss / Take Profit / horizonte semanal / guardia de fin
de semana) NO se deciden aca: son responsabilidad de
risk.risk_manager.RiskManager.evaluate_position_exit() (unica fuente de
verdad de "cuando cerrar" - ver ese modulo). build_exit_signals() de aca es
solo el glue que recorre el portafolio y arma la señal de salida.

NOTA DE RIESGO (leer antes de operar con capital real): el objetivo de
retorno semanal configurado (LongFirstConfig.weekly_target_ars) es un
PARAMETRO DE DIMENSIONAMIENTO para calibrar cuanta convexidad se busca por
trade, NO una proyeccion ni una garantia. Un objetivo de 100% de retorno
semanal implica, por construccion matematica, arriesgar una fraccion
grande del capital en estructuras que pueden perder la totalidad de la
prima pagada si la volatilidad esperada no se materializa. Nada en este
modulo estima la probabilidad de alcanzar ese objetivo - eso depende del
mercado, no de la configuracion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionChain, OptionQuote, OrderBookSnapshot
from ggal_bot.data.technical_analysis import MomentumShift, Trend
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.models.microstructure import passes_obi_filter
from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.portfolio.portfolio import Portfolio
from ggal_bot.risk.risk_manager import RiskManager


def _estimate_execution_cost_pct(
    book: OrderBookSnapshot, impact_coefficient: float = 0.02,
) -> Optional[float]:
    """
    Estimacion de costo de ejecucion (MEJORA 2026-09-28, ver
    config.LongFirstConfig.enable_execution_cost_filter), como fraccion de
    la prima (mid): mitad del spread relativo (lo que se paga siempre al
    cruzar al ask desde el mid) mas un termino de impacto que crece cuando
    el tamaño de punta mostrado (ask_size) es chico - proxy de cuanto se
    moveria el precio si el tamaño realmente operado excede lo mostrado,
    NO una medicion exacta (ver LIMITACION en config.py: al momento del
    scan todavia no se conoce la cantidad final de contratos). None si el
    book no tiene mid valido (bid/ask invalidos).
    """
    if book.mid <= 0 or book.ask_size <= 0:
        return None
    half_spread_pct = (book.spread / 2.0) / book.mid
    impact_pct = impact_coefficient / book.ask_size
    return half_spread_pct + impact_pct


@dataclass
class EntrySignal:
    symbol: str
    option_type: OptionType
    action: str = "buy_to_open"      # unica accion de apertura permitida bajo este modo
    reason: str = ""
    iv_dislocation_vol_points: float = 0.0
    premium_reference: float = 0.0     # mid vigente, para dimensionar con risk.position_sizer.PositionSizer
    days_business_to_expiry: int = 0
    convexity_score: float = 0.0       # (|gamma| + |vega|/100) / prima; mayor = mas convexidad por peso pagado
    trend_context: str = ""            # lectura de data/technical_analysis.py vigente al momento de la señal


@dataclass
class SpreadCompletionSignal:
    long_symbol: str
    short_symbol: str
    option_type: OptionType
    action: str = "sell_to_open_wing"   # pata corta de un spread YA financiado por una larga confirmada
    reason: str = ""
    long_quantity_confirmed: float = 0.0  # cantidad larga ya en portafolio (nunca se vende mas que esto)
    trend_context: str = ""            # lectura de data/technical_analysis.py vigente al momento de la señal


@dataclass
class SpreadOpenSignal:
    """
    MEJORA 2026-09-17 (a pedido explicito del usuario, conversacion sobre el
    indicador SuperTrend AI del 2026-09-10): "si el ratio [IV implicita ATM
    actual / volatilidad realizada] esta alto, la prima esta cara relativo a
    lo que el activo realmente se mueve - ahi conviene un spread de debito
    (comprar el strike cercano, vender uno mas lejano) en vez de la opcion
    simple, para pagar menos theta". Distinto de SpreadCompletionSignal: ese
    otro solo agrega la pata corta a una LARGA YA CONFIRMADA en el
    portafolio (financiamiento/cap defensivo); este abre AMBAS patas de
    una, como entrada nueva, cuando la dislocacion de smile indica IV cara
    en vez de barata (ver WeeklyAsymmetricStrategy.
    scan_expensive_iv_spread_signals). Ver
    config.LongFirstConfig.enable_expensive_iv_spread_entry (apagado por
    defecto).
    """
    long_symbol: str
    short_symbol: str
    option_type: OptionType
    action: str = "open_debit_spread"
    reason: str = ""
    iv_dislocation_vol_points: float = 0.0
    net_debit_premium: float = 0.0     # prima larga - prima corta (mid); lo que realmente se paga por spread
    days_business_to_expiry: int = 0
    trend_context: str = ""


@dataclass
class ExitSignal:
    symbol: str
    reason: str            # "stop_loss" | "take_profit" | "weekly_horizon_expired" | "weekend_theta_guard" | "vega_theta_decay" | "partial_profit_take"
    action: str = "sell_to_close"
    quantity: float = 0.0


@dataclass
class CandidateFunnelRecord:
    """
    Un registro POR CANDIDATA evaluada en un scan_entry_signals() (MEJORA
    2026-09-29, a pedido explicito del usuario: "logger de embudo
    estructurado (universo completo de candidatas por ciclo con spread,
    profundidad, griegas, delta y que filtros paso cada una)"). Opt-in
    (ver LongFirstConfig.enable_signal_funnel_log, default False) - cuando
    esta apagado, `EntryScanDiagnostics.candidate_funnel` queda vacio y
    esta clase no se instancia, sin ningun costo adicional sobre el
    comportamiento pre-existente (EntryScanDiagnostics agregado, sin
    detalle por candidata, sigue calculandose exactamente igual).

    `blocked_at`: nombre del PRIMER filtro que descarto esta candidata
    (mismo orden secuencial que los contadores de EntryScanDiagnostics,
    ver el bucle de scan_entry_signals) o None si califico (genero
    EntrySignal). Como los filtros son secuenciales con "continue"
    temprano, "que filtros paso" = todos los anteriores a `blocked_at` en
    ese mismo orden - no hace falta un dict aparte por filtro.

    Todos los campos de mercado/griegas vienen DIRECTO de OptionQuote/
    OrderBookSnapshot en el momento de la evaluacion - nunca se fabrica un
    valor: si `q.greeks`/`q.iv` todavia no estaban calculados este ciclo,
    quedan en None tal cual.
    """
    symbol: str
    option_type: str
    strike: float
    expiry: Optional[date]
    days_business: int
    spot_ref: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    spread_abs: Optional[float]
    spread_relative: Optional[float]
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    vega: Optional[float]
    theta: Optional[float]
    dislocation_vol_points: Optional[float]
    blocked_at: Optional[str]  # None = califico (qualified)


@dataclass
class EntryScanDiagnostics:
    """
    Diagnostico PURO de scan_entry_signals(): NO cambia ningun umbral ni
    comportamiento, solo cuenta en que filtro se descarta cada quote
    candidata y guarda la dislocacion MAS CERCANA a calificar entre las
    que llegaron al chequeo de smile sin alcanzar el umbral vigente.
    Agregado a pedido explicito (ver seguimiento de auditoria del
    2026-09-01) tras la duda de si los filtros son "muy duros": con el
    deploy corriendo apenas unas horas y la tendencia 1D leyendo NEUTRAL
    de forma sostenida (lo que ya DUPLICA el umbral de dislocacion exigido,
    ver TechnicalAnalysisConfig.neutral_extreme_smile_multiplier), no habia
    forma de distinguir "los umbrales estan mal calibrados" de "el mercado
    todavia no presento una dislocacion que los mercados en NEUTRAL exigen"
    - antes esta informacion se descartaba en silencio en cada `continue`.
    Se guarda en WeeklyAsymmetricStrategy.last_scan_diagnostics (no se
    retorna junto con las señales para no romper la firma/tests
    existentes de scan_entry_signals) para que run_bot.py la loguee,
    throttleada, sin que este modulo deje de estar libre de I/O.
    """
    total_quotes: int = 0
    blocked_by_direction: int = 0       # smile_threshold None: bloqueo direccional tecnico (BULLISH/BEARISH sin reversion)
    blocked_by_holding_days: int = 0
    blocked_by_min_days_to_expiry: int = 0  # ver config.LongFirstConfig.min_business_days_to_expiry_for_entry
    blocked_by_liquidity: int = 0
    blocked_by_obi: int = 0
    blocked_by_moneyness: int = 0
    blocked_by_delta_band: int = 0      # ver config.LongFirstConfig.enable_delta_band_filter
    evaluated_for_dislocation: int = 0  # llegaron al chequeo de smile (pasaron todos los filtros anteriores)
    blocked_by_dislocation: int = 0     # llegaron pero no alcanzaron el umbral vigente (normal o extremo bajo NEUTRAL)
    blocked_by_zscore: int = 0          # MEJORA 2026-09-28: ver config.LongFirstConfig.enable_zscore_filter
    blocked_by_execution_cost: int = 0  # MEJORA 2026-09-28: ver config.LongFirstConfig.enable_execution_cost_filter
    blocked_by_earnings_blackout: int = 0  # MEJORA 2026-09-28: ver config.LongFirstConfig.enable_earnings_blackout
    blocked_by_weekend_entry_guard: int = 0  # FIX 2026-09-29: ver config.LongFirstConfig.weekend_theta_guard_block_new_entries
    qualified: int = 0                  # generaron EntrySignal
    trend: str = ""
    closest_miss_symbol: Optional[str] = None
    closest_miss_dislocation: Optional[float] = None          # dislocation real observada (mas negativo = mas barata)
    closest_miss_threshold_required: Optional[float] = None   # -smile_threshold exigido para esa opcion puntual
    closest_miss_shortfall_vol_points: Optional[float] = None  # cuanto le falto en puntos de vol (siempre >= 0)
    candidate_funnel: List[CandidateFunnelRecord] = field(default_factory=list)  # ver LongFirstConfig.enable_signal_funnel_log


class WeeklyAsymmetricStrategy:
    def __init__(self, risk_manager: RiskManager, config=None):
        self.risk_manager = risk_manager
        self.cfg = config if config is not None else SETTINGS.long_first
        # Ver EntryScanDiagnostics: se sobreescribe en cada scan_entry_signals().
        self.last_scan_diagnostics: Optional[EntryScanDiagnostics] = None

    # -- Entradas: unicamente BUY to Open, en bases baratas y de horizonte semanal --

    def scan_entry_signals(
        self,
        surface: VolatilitySurface,
        recent_volumes: Dict[str, float],
        hv_estimate: Optional[float] = None,
        trend: str = Trend.NEUTRAL.value,
        momentum_shift: Optional[str] = None,
        dislocation_zscore: Optional[Dict[str, float]] = None,
        earnings_blackout: bool = False,
        adr_ccl_trend: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> List[EntrySignal]:
        """
        `trend`: lectura vigente de data.technical_analysis.get_daily_trend_signal()
        ("BULLISH"|"BEARISH"|"NEUTRAL"), inyectada por el llamador (run_bot.py
        via TechnicalAnalysisEngine) - ver la nota de diseño en el docstring
        del modulo. Default NEUTRAL (el mas conservador: exige dislocacion
        extrema) para quien llame a este metodo sin pasar una lectura tecnica.

        `momentum_shift`: lectura opcional de
        data.technical_analysis.TechnicalSnapshot.momentum_shift (ver
        MomentumShift), inyectada igual que `trend` (mismo TechnicalSnapshot,
        mismo ciclo). Cuando indica una reversion temprana EN CONTRA de
        `trend` (ej. trend=BEARISH y momentum_shift=EARLY_BULLISH_REVERSAL),
        el option_type contrario a `trend` deja de descartarse de plano: se
        vuelve a evaluar, pero exigiendo el umbral EXTREMO de dislocacion de
        smile (el mismo que ya rige bajo NEUTRAL) en vez del normal - se
        relaja la prohibicion estricta sin resignar la disciplina de
        tendencia (ver docstring del modulo y config.TechnicalAnalysisConfig).

        `dislocation_zscore` (MEJORA 2026-09-28, ver
        config.LongFirstConfig.enable_zscore_filter y
        data/dislocation_history.py::DislocationHistoryTracker): dict
        {symbol: z-score} ya calculado por el llamador (mismo patron de
        inyeccion que `trend` - este modulo sigue sin guardar ningun estado
        propio). Con el flag apagado (default), este parametro se ignora
        por completo.

        `earnings_blackout` (MEJORA 2026-09-28, ver
        config.LongFirstConfig.enable_earnings_blackout): True bloquea
        TODA entrada nueva de este scan (calculado por el llamador contra
        `earnings_dates`/`earnings_blackout_days_before` - este modulo no
        conoce ninguna fecha de calendario, solo el booleano ya resuelto).

        `adr_ccl_trend` (MEJORA 2026-09-28, HYPOTHESIS no verificada, ver
        config.LongFirstConfig.enable_adr_ccl_filter): lectura direccional
        externa opcional ("BULLISH"|"BEARISH"|"NEUTRAL", mismo formato que
        `trend`) derivada del ADR (NYSE:GGAL) y el dolar CCL implicito -
        este modulo NO la calcula ni la obtiene, solo la consume si el
        llamador la inyecta. Cuando esta habilitado y discrepa de `trend`
        para un `option_type` dado, exige el umbral EXTREMO en vez del
        normal (mismo patron que Momentum Shift), nunca bloquea de plano.

        `now` (FIX 2026-09-29, ver config.LongFirstConfig.
        weekend_theta_guard_block_new_entries y REPORT.md §4.0/§9.0):
        fecha/hora vigente, inyectada por el llamador exactamente igual que
        `trend` (mismo criterio de diseño del docstring del modulo: este
        metodo sigue sin llamar datetime.now() internamente). Se usa
        UNICAMENTE para, con el flag de arriba activado, no generar una
        entrada nueva un viernes sobre un vencimiento posterior a ese
        viernes cuando weekend_theta_guard_enabled esta activo - esa
        posicion quedaria con holding_business_days=0 y el guard de salida
        la cerraria casi de inmediato en el siguiente ciclo de riesgo (ver
        risk/risk_manager.py::evaluate_position_exit). Con `now=None`
        (default) o el flag apagado, este chequeo no se aplica: identico
        al comportamiento de siempre.
        """
        cfg = self.cfg
        ta_cfg = SETTINGS.technical_analysis

        diag = EntryScanDiagnostics(total_quotes=len(surface.quotes), trend=trend)
        if earnings_blackout and getattr(cfg, "enable_earnings_blackout", False):
            diag.blocked_by_earnings_blackout = len(surface.quotes)
            if getattr(cfg, "enable_signal_funnel_log", False):
                for q in surface.quotes:
                    greeks = q.greeks or {}
                    bid, ask = q.book.bid, q.book.ask
                    spread_abs = (ask - bid) if (ask is not None and bid is not None) else None
                    mid = q.book.mid
                    diag.candidate_funnel.append(CandidateFunnelRecord(
                        symbol=q.symbol, option_type=getattr(q.option_type, "value", q.option_type),
                        strike=q.strike, expiry=q.expiry, days_business=q.days_business,
                        spot_ref=q.spot_ref, bid=bid, ask=ask,
                        bid_size=q.book.bid_size, ask_size=q.book.ask_size,
                        spread_abs=spread_abs,
                        spread_relative=(spread_abs / mid) if (spread_abs is not None and mid) else None,
                        iv=q.iv, delta=greeks.get("delta"), gamma=greeks.get("gamma"),
                        vega=greeks.get("vega"), theta=greeks.get("theta"),
                        dislocation_vol_points=None, blocked_at="earnings_blackout",
                    ))
            self.last_scan_diagnostics = diag
            return []

        level_ok = True
        if cfg.require_level_confirmation and hv_estimate is not None:
            level_ok = surface.level_dislocation(hv_estimate) < -cfg.level_threshold_vol_points

        # Filtro direccional tecnico obligatorio (requerimiento funcional):
        # bajo NEUTRAL no se descarta ningun option_type de antemano, pero
        # se exige una dislocacion de smile EXTREMA (ver docstring del
        # modulo); bajo BULLISH/BEARISH se descarta el option_type contrario
        # -salvo que Momentum Shift indique una reversion temprana en esa
        # direccion (ver docstring de arriba), caso en el que se lo vuelve a
        # admitir bajo el umbral EXTREMO en lugar del normal.
        normal_threshold = cfg.smile_threshold_vol_points
        extreme_threshold = cfg.smile_threshold_vol_points * ta_cfg.neutral_extreme_smile_multiplier

        momentum_override_type: Optional[OptionType] = None
        if ta_cfg.enabled and ta_cfg.enable_momentum_shift_override:
            if trend == Trend.BEARISH.value and momentum_shift == MomentumShift.EARLY_BULLISH_REVERSAL.value:
                momentum_override_type = OptionType.CALL  # contrario a BEARISH
            elif trend == Trend.BULLISH.value and momentum_shift == MomentumShift.EARLY_BEARISH_REVERSAL.value:
                momentum_override_type = OptionType.PUT  # contrario a BULLISH

        # ADR/CCL (MEJORA 2026-09-28, ver docstring de arriba y
        # config.LongFirstConfig.enable_adr_ccl_filter): "lado natural" que
        # esa lectura externa respalda, si esta habilitada y provista.
        adr_ccl_supported_type: Optional[OptionType] = None
        if getattr(cfg, "enable_adr_ccl_filter", False) and adr_ccl_trend:
            if adr_ccl_trend == Trend.BULLISH.value:
                adr_ccl_supported_type = OptionType.CALL
            elif adr_ccl_trend == Trend.BEARISH.value:
                adr_ccl_supported_type = OptionType.PUT

        def _smile_threshold_for(option_type: OptionType) -> Optional[float]:
            """
            Umbral de dislocacion de smile a exigir para `option_type` bajo
            la tendencia/momentum vigentes, o None si `option_type` debe
            descartarse de plano (bloqueo direccional estricto, sin
            reversion temprana que lo habilite).
            """
            if not ta_cfg.enabled:
                threshold = normal_threshold  # filtro tecnico desactivado por config: comportamiento pre-modulo
            elif trend == Trend.BULLISH.value:
                if option_type is OptionType.CALL:
                    threshold = normal_threshold
                else:
                    return extreme_threshold if momentum_override_type is option_type else None
            elif trend == Trend.BEARISH.value:
                if option_type is OptionType.PUT:
                    threshold = normal_threshold
                else:
                    return extreme_threshold if momentum_override_type is option_type else None
            else:
                threshold = extreme_threshold  # NEUTRAL

            # ADR/CCL discrepa de `trend` para este option_type: endurece a
            # extremo en vez de bloquear - nunca afloja nada (si `threshold`
            # ya era extreme_threshold via NEUTRAL/momentum override, esto
            # no lo cambia).
            if (
                adr_ccl_supported_type is not None
                and adr_ccl_supported_type is not option_type
                and threshold < extreme_threshold
            ):
                threshold = extreme_threshold
            return threshold

        funnel_enabled = getattr(cfg, "enable_signal_funnel_log", False)

        def _funnel_record(q, blocked_at: Optional[str], dislocation_value: Optional[float] = None) -> CandidateFunnelRecord:
            greeks = q.greeks or {}
            bid, ask = q.book.bid, q.book.ask
            spread_abs = (ask - bid) if (ask is not None and bid is not None) else None
            mid = q.book.mid
            spread_relative = (spread_abs / mid) if (spread_abs is not None and mid) else None
            return CandidateFunnelRecord(
                symbol=q.symbol, option_type=getattr(q.option_type, "value", q.option_type),
                strike=q.strike, expiry=q.expiry, days_business=q.days_business,
                spot_ref=q.spot_ref, bid=bid, ask=ask,
                bid_size=q.book.bid_size, ask_size=q.book.ask_size,
                spread_abs=spread_abs, spread_relative=spread_relative,
                iv=q.iv,
                delta=greeks.get("delta"), gamma=greeks.get("gamma"),
                vega=greeks.get("vega"), theta=greeks.get("theta"),
                dislocation_vol_points=dislocation_value, blocked_at=blocked_at,
            )

        def _record_block(q, stage: str) -> None:
            if funnel_enabled:
                diag.candidate_funnel.append(_funnel_record(q, blocked_at=stage))

        candidates: List[EntrySignal] = []
        for q in surface.quotes:
            smile_threshold = _smile_threshold_for(q.option_type)
            if smile_threshold is None:
                diag.blocked_by_direction += 1
                _record_block(q, "direction")
                continue  # filtro direccional tecnico: bajo BULLISH/BEARISH sin reversion temprana, ni se evalua

            # Horizonte de entrada: nunca se abre una posicion que exceda el
            # maximo de ruedas habiles configurado, aunque este muy barata.
            # cfg.max_holding_business_days puede ser None ("sin limite",
            # ver LongFirstConfig/ScalpingConfig en config.py, AJUSTE
            # 2026-09-07 a pedido explicito del usuario) - en ese caso
            # ninguna cotizacion se descarta por este motivo, sin importar
            # que tan lejano sea su vencimiento.
            if cfg.max_holding_business_days is not None and q.days_business > cfg.max_holding_business_days:
                diag.blocked_by_holding_days += 1
                _record_block(q, "holding_days")
                continue

            # Piso de vencimiento minimo (MEJORA 2026-09-17, ver
            # config.LongFirstConfig.min_business_days_to_expiry_for_entry):
            # complementa al filtro de arriba (que descarta vencimientos
            # DEMASIADO LEJANOS) descartando tambien los DEMASIADO CERCANOS
            # para tener mercado real - getattr() defensivo porque
            # ScalpingConfig podria no tener el atributo si se agrega en el
            # futuro un caller que reutilice esta funcion sin ese campo.
            min_days_to_expiry = getattr(cfg, "min_business_days_to_expiry_for_entry", None)
            if min_days_to_expiry is not None and q.days_business < min_days_to_expiry:
                diag.blocked_by_min_days_to_expiry += 1
                _record_block(q, "min_days_to_expiry")
                continue

            # Weekend theta guard coordinado con la entrada (FIX 2026-09-29,
            # ver docstring de `now` arriba y config.LongFirstConfig.
            # weekend_theta_guard_block_new_entries): opt-in, default False.
            # Espeja la condicion exacta bajo la que
            # risk_manager.evaluate_position_exit() va a cerrar esta misma
            # posicion por "weekend_theta_guard" si se abriera ahora (con
            # holding_business_days=0 recien abierta) - NO afloja el guard
            # de salida, solo evita abrir algo que el guard va a cerrar el
            # mismo dia.
            if (
                now is not None
                and cfg.weekend_theta_guard_enabled
                and getattr(cfg, "weekend_theta_guard_block_new_entries", False)
                and now.weekday() == 4
                and q.expiry > now.date()
                and (
                    cfg.weekend_theta_guard_max_holding_business_days is None
                    or 0 < cfg.weekend_theta_guard_max_holding_business_days
                )
            ):
                diag.blocked_by_weekend_entry_guard += 1
                _record_block(q, "weekend_entry_guard")
                continue

            volume = recent_volumes.get(q.symbol, 0.0)
            if not self.risk_manager.check_liquidity(q.book, volume):
                diag.blocked_by_liquidity += 1
                _record_block(q, "liquidity")
                continue

            # Confirmacion de microestructura (ver models/microstructure.py):
            # filtro de CALIDAD DE EJECUCION, no de alpha direccional - evita
            # levantar la oferta justo cuando el libro muestra un desbalance
            # extremo hacia el lado vendedor (tipico de una punta
            # aislada/iliquida en un libro tan delgado como el de GGAL).
            if cfg.enable_obi_filter and not passes_obi_filter(q.book, cfg.min_obi_for_entry):
                diag.blocked_by_obi += 1
                _record_block(q, "obi")
                continue

            if not q.spot_ref or q.spot_ref <= 0:
                _record_block(q, "invalid_spot_ref")
                continue
            log_moneyness = math.log(q.strike / q.spot_ref)
            if abs(log_moneyness) > cfg.moneyness_band_pct:
                diag.blocked_by_moneyness += 1
                _record_block(q, "moneyness")
                continue  # fuera de la banda ATM/OTM cercana (convexidad objetivo)

            # Filtro ADICIONAL por banda de delta (MEJORA 2026-09-17, ver
            # config.LongFirstConfig.enable_delta_band_filter) - apagado por
            # defecto, se SUMA al filtro de moneyness de arriba (no lo
            # reemplaza) cuando esta habilitado. Una base sin Griegas
            # calculadas todavia (q.greeks is None) se descarta por este
            # filtro en vez de admitirse a ciegas - mismo criterio
            # conservador que el resto de los filtros de calidad de este
            # metodo.
            if getattr(cfg, "enable_delta_band_filter", False):
                delta = abs((q.greeks or {}).get("delta", 0.0)) if q.greeks is not None else None
                if delta is None or not (cfg.delta_band_min <= delta <= cfg.delta_band_max):
                    diag.blocked_by_delta_band += 1
                    _record_block(q, "delta_band")
                    continue

            diag.evaluated_for_dislocation += 1
            dislocation = surface.smile_dislocation(q)
            if dislocation >= -smile_threshold:
                diag.blocked_by_dislocation += 1
                # Cuanto le falto en puntos de vol para calificar (siempre >= 0)
                # y si es el "menos lejos" visto en este ciclo, se guarda como
                # el closest miss - dato real para juzgar si el umbral vigente
                # es razonable, sin tener que aflojarlo a ciegas.
                shortfall = dislocation - (-smile_threshold)
                if (
                    diag.closest_miss_shortfall_vol_points is None
                    or shortfall < diag.closest_miss_shortfall_vol_points
                ):
                    diag.closest_miss_symbol = q.symbol
                    diag.closest_miss_dislocation = dislocation
                    diag.closest_miss_threshold_required = -smile_threshold
                    diag.closest_miss_shortfall_vol_points = shortfall
                if funnel_enabled:
                    diag.candidate_funnel.append(_funnel_record(q, blocked_at="dislocation", dislocation_value=dislocation))
                continue  # no esta "barata" (o no lo suficiente bajo NEUTRAL): NUNCA se genera señal de venta para abrir
            if not level_ok:
                if funnel_enabled:
                    diag.candidate_funnel.append(_funnel_record(q, blocked_at="level_confirmation", dislocation_value=dislocation))
                continue

            # Filtro de dislocacion RELATIVA por z-score (MEJORA 2026-09-28,
            # ver docstring de arriba y config.LongFirstConfig.
            # enable_zscore_filter): SE SUMA al umbral fijo de arriba, nunca
            # lo reemplaza - una base ya paso smile_threshold_vol_points en
            # puntos absolutos, esto exige ADEMAS que sea anomala contra su
            # propia ventana reciente. Sin historia suficiente todavia
            # (z is None) se descarta por este filtro (ausencia de
            # informacion nunca abre riesgo nuevo), igual que cualquier
            # otro filtro de calidad de este metodo.
            if getattr(cfg, "enable_zscore_filter", False):
                z = (dislocation_zscore or {}).get(q.symbol)
                if z is None or z > -cfg.zscore_threshold:
                    diag.blocked_by_zscore += 1
                    if funnel_enabled:
                        diag.candidate_funnel.append(_funnel_record(q, blocked_at="zscore", dislocation_value=dislocation))
                    continue

            premium = q.book.mid
            if premium <= 0:
                if funnel_enabled:
                    diag.candidate_funnel.append(_funnel_record(q, blocked_at="invalid_premium", dislocation_value=dislocation))
                continue

            # Costo de ejecucion estimado (MEJORA 2026-09-28, ver docstring
            # de config.LongFirstConfig.enable_execution_cost_filter):
            # descarta una base cuyo costo esperado de cruzar el spread +
            # impacto por tamaño de punta chico ya se comeria una fraccion
            # relevante de la prima, aunque la dislocacion de IV sea real.
            if getattr(cfg, "enable_execution_cost_filter", False):
                execution_cost_pct = _estimate_execution_cost_pct(
                    q.book, impact_coefficient=cfg.execution_cost_impact_coefficient,
                )
                if execution_cost_pct is None or execution_cost_pct > cfg.execution_cost_max_pct:
                    diag.blocked_by_execution_cost += 1
                    if funnel_enabled:
                        diag.candidate_funnel.append(_funnel_record(q, blocked_at="execution_cost", dislocation_value=dislocation))
                    continue

            greeks = q.greeks or {}
            convexity_score = (abs(greeks.get("gamma", 0.0)) + abs(greeks.get("vega", 0.0)) / 100.0) / premium

            is_momentum_override = momentum_override_type is q.option_type
            reason = (
                f"IV cruda {dislocation:.2f} vol pts por debajo de la curva "
                f"(horizonte semanal: {q.days_business}d habiles; tendencia 1D: {trend}"
            )
            if is_momentum_override:
                reason += f"; MOMENTUM OVERRIDE ({momentum_shift}): contrarian a la tendencia bajo umbral extremo"
            reason += ")"

            candidates.append(EntrySignal(
                symbol=q.symbol, option_type=q.option_type,
                reason=reason,
                iv_dislocation_vol_points=dislocation, premium_reference=premium,
                days_business_to_expiry=q.days_business, convexity_score=convexity_score,
                trend_context=trend,
            ))
            if funnel_enabled:
                diag.candidate_funnel.append(_funnel_record(q, blocked_at=None, dislocation_value=dislocation))

        candidates.sort(key=lambda s: s.convexity_score, reverse=True)
        diag.qualified = len(candidates)
        self.last_scan_diagnostics = diag
        return candidates

    # -- Spread de debito como ENTRADA NUEVA cuando la IV esta CARA (MEJORA 2026-09-17) --

    def scan_expensive_iv_spread_signals(
        self,
        surface: VolatilitySurface,
        option_chain: OptionChain,
        recent_volumes: Dict[str, float],
        trend: str = Trend.NEUTRAL.value,
        max_quote_age_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> List[SpreadOpenSignal]:
        """
        Ver config.LongFirstConfig.enable_expensive_iv_spread_entry para la
        motivacion completa. Complementa scan_entry_signals() (que solo
        actua sobre dislocacion NEGATIVA, "IV barata"): esta busca
        dislocacion POSITIVA ("IV cara") y, si encuentra una base candidata
        Y un wing valido para armarle spread (_find_wing_quote, la misma
        logica ya usada por scan_spread_completion_signals), arma AMBAS
        patas como entrada nueva - no requiere ninguna posicion previa en
        el portafolio (a diferencia de scan_spread_completion_signals).
        Apagado por defecto (`self.cfg.enable_expensive_iv_spread_entry`).

        Bajo NEUTRAL no se genera ninguna señal (mismo criterio que
        scan_spread_completion_signals: sin conviccion direccional no hay
        base para asumir el riesgo direccional neto que todavia conserva un
        spread de debito, aunque acotado).
        """
        cfg = self.cfg
        if not getattr(cfg, "enable_expensive_iv_spread_entry", False):
            return []
        ta_cfg = SETTINGS.technical_analysis
        if ta_cfg.enabled and trend == Trend.NEUTRAL.value:
            return []

        allowed_option_type: Optional[OptionType] = None
        if ta_cfg.enabled and trend == Trend.BULLISH.value:
            allowed_option_type = OptionType.CALL
        elif ta_cfg.enabled and trend == Trend.BEARISH.value:
            allowed_option_type = OptionType.PUT

        threshold = getattr(cfg, "expensive_iv_spread_threshold_vol_points", 3.0)
        min_days_to_expiry = getattr(cfg, "min_business_days_to_expiry_for_entry", None)

        signals: List[SpreadOpenSignal] = []
        for q in surface.quotes:
            if allowed_option_type is not None and q.option_type is not allowed_option_type:
                continue
            if cfg.max_holding_business_days is not None and q.days_business > cfg.max_holding_business_days:
                continue
            if min_days_to_expiry is not None and q.days_business < min_days_to_expiry:
                continue
            volume = recent_volumes.get(q.symbol, 0.0)
            if not self.risk_manager.check_liquidity(q.book, volume):
                continue
            if cfg.enable_obi_filter and not passes_obi_filter(q.book, cfg.min_obi_for_entry):
                continue
            if not q.spot_ref or q.spot_ref <= 0:
                continue
            if abs(math.log(q.strike / q.spot_ref)) > cfg.moneyness_band_pct:
                continue

            dislocation = surface.smile_dislocation(q)
            if dislocation <= threshold:
                continue  # no esta lo suficientemente "cara" para justificar pagar menos theta con un spread

            wing = self._find_wing_quote(
                option_chain, q, cfg, max_quote_age_seconds=max_quote_age_seconds, now=now,
            )
            if wing is None or wing.book.bid <= 0 or wing.book.ask <= 0:
                continue

            net_debit = q.book.mid - wing.book.mid
            if net_debit <= 0:
                continue  # spread degenerado (credito neto en vez de debito) - no es el patron que se busca aca

            spread_kind = "Bull Call Spread" if q.option_type is OptionType.CALL else "Bear Put Spread"
            signals.append(SpreadOpenSignal(
                long_symbol=q.symbol, short_symbol=wing.symbol, option_type=q.option_type,
                reason=(
                    f"{spread_kind} (entrada nueva): IV cruda {dislocation:.2f} vol pts por encima de "
                    f"la curva (cara) - se paga {net_debit:.2f} de debito neto en vez de la prima "
                    f"simple {q.book.mid:.2f} para pagar menos theta (horizonte semanal: "
                    f"{q.days_business}d habiles; tendencia 1D: {trend})"
                ),
                iv_dislocation_vol_points=dislocation, net_debit_premium=net_debit,
                days_business_to_expiry=q.days_business, trend_context=trend,
            ))
        return signals

    # -- Spreads: la pata corta SOLO si la larga ya esta confirmada en portafolio --

    def scan_spread_completion_signals(
        self, option_chain: OptionChain, portfolio: Portfolio, trend: str = Trend.NEUTRAL.value,
        max_quote_age_seconds: Optional[float] = None, now: Optional[float] = None,
        forced_expiry: Optional[date] = None, strategy_tag: str = "weekly_asymmetric",
    ) -> List[SpreadCompletionSignal]:
        """
        `trend`: misma lectura inyectada que scan_entry_signals(). Bajo
        NEUTRAL no se completa ningun spread (cash/espera estricto - ver
        docstring del modulo); bajo BULLISH/BEARISH solo se completan
        spreads del option_type consistente con la tendencia (Bull Call
        Spread bajo BULLISH, Bear Put Spread bajo BEARISH), aunque exista
        una larga confirmada del tipo contrario (ej. una Put comprada en un
        regimen BEARISH anterior no se "spreadea" si la tendencia ya paso a
        BULLISH - la pata larga sigue gestionada por build_exit_signals(),
        solo se le niega la pata corta nueva).

        `max_quote_age_seconds`/`now` (BUG REAL CORREGIDO, ver
        RiskConfig.max_option_quote_staleness_seconds y el docstring de
        _find_wing_quote): cierra el ultimo hueco del "paso 3" del ciclo -
        antes, la pata corta (`wing`) que financia el spread se elegia sin
        mirar que tan fresca era su punta, a diferencia de las entradas
        nuevas del paso 2 (ver run_bot.py:_run_weekly_asymmetric_cycle,
        que ya excluye opciones stale de `valid_quotes`). Mismo motivo que
        alla: comprometerse a vender una pata corta contra un precio de hace
        rato (cadena de opciones caida sola, spot fresco - ver
        docs/AUDITORIA_MAESTRA_2026-08-27.md, seguimiento del 2026-08-31) es
        exactamente el tipo de decision que esta guardia existe para evitar.
        Se inyectan (no se llama time.time() aca adentro) por el mismo
        criterio que el resto del modulo: libre de I/O, testeable con
        datos sinteticos. Default None = sin filtro de staleness (compatible
        hacia atras con cualquier llamador que no los pase).

        `forced_expiry` (a pedido explicito del usuario, 2026-09-01 - ver
        InstrumentsConfig.forced_expiry): si se pasa, se ignoran por
        completo las bases de cualquier OTRO vencimiento (ni como pata
        larga confirmada ni como candidata a wing) - mismo criterio que el
        filtro equivalente en run_bot.py:_run_weekly_asymmetric_cycle para
        entradas nuevas. Default None = sin filtro (compatible hacia atras).

        `strategy_tag` (ver portfolio.Position.strategy_tag y el modo
        Scalping ADITIVO en config.ScalpingConfig): solo se consideran
        posiciones largas confirmadas con esta marca (una posicion sin
        marca, es decir `None`, cuenta como "weekly_asymmetric" - mismo
        criterio que _confirmed_long_quantity) - evita que este metodo
        arme una pata corta sobre una base cuya larga en realidad la abrio
        el modo Scalping bajo sus propias reglas. Default
        "weekly_asymmetric" preserva el comportamiento previo a este
        parametro para cualquier llamador que no lo pase.
        """
        if not self.cfg.enable_spread_completion:
            return []

        ta_cfg = SETTINGS.technical_analysis
        if ta_cfg.enabled and trend == Trend.NEUTRAL.value:
            return []

        allowed_option_type: Optional[OptionType] = None
        if ta_cfg.enabled and trend == Trend.BULLISH.value:
            allowed_option_type = OptionType.CALL
        elif ta_cfg.enabled and trend == Trend.BEARISH.value:
            allowed_option_type = OptionType.PUT

        signals: List[SpreadCompletionSignal] = []
        for quote in option_chain.all_quotes():
            if forced_expiry is not None and quote.expiry != forced_expiry:
                continue  # vencimiento forzado: se ignora cualquier otro por completo
            if allowed_option_type is not None and quote.option_type is not allowed_option_type:
                continue  # filtro direccional tecnico: no se agrega exposicion contraria a la tendencia vigente

            long_qty = self._confirmed_long_quantity(portfolio, quote.symbol, strategy_tag=strategy_tag)
            if long_qty <= 0:
                # Invariante central de este modulo: sin una posicion larga
                # ya confirmada en el portafolio para ESTA base especifica,
                # no hay ninguna ruta que arme una pata corta sobre ella.
                continue

            wing = self._find_wing_quote(
                option_chain, quote, self.cfg,
                max_quote_age_seconds=max_quote_age_seconds, now=now,
            )
            if wing is None or wing.book.bid <= 0 or wing.book.ask <= 0:
                continue

            spread_kind = "Bull Call Spread" if quote.option_type is OptionType.CALL else "Bear Put Spread"
            signals.append(SpreadCompletionSignal(
                long_symbol=quote.symbol, short_symbol=wing.symbol, option_type=quote.option_type,
                reason=f"{spread_kind}: financiar/capear la pata larga ya confirmada ({quote.symbol}, qty={long_qty:g})",
                long_quantity_confirmed=long_qty, trend_context=trend,
            ))
        return signals

    @staticmethod
    def _confirmed_long_quantity(portfolio: Portfolio, symbol: str, strategy_tag: str = "weekly_asymmetric") -> float:
        """
        Suma solo exposicion LARGA (quantity > 0) de `symbol` con la marca
        `strategy_tag` - nunca cuenta posiciones cortas ni posiciones
        abiertas por OTRA estrategia (ver portfolio.Position.strategy_tag y
        el modo Scalping ADITIVO en config.ScalpingConfig). Una posicion sin
        marca (`strategy_tag is None`, el caso de toda posicion abierta
        antes de que este campo existiera) cuenta como "weekly_asymmetric".
        """
        return sum(
            p.quantity for p in portfolio.positions
            if p.symbol == symbol and p.quantity > 0 and (p.strategy_tag or "weekly_asymmetric") == strategy_tag
        )

    @staticmethod
    def _find_wing_quote(
        option_chain: OptionChain, long_quote: OptionQuote, cfg=None,
        max_quote_age_seconds: Optional[float] = None, now: Optional[float] = None,
    ) -> Optional[OptionQuote]:
        """
        Entre las bases del mismo tipo y vencimiento, busca la mas cercana
        por AFUERA del strike largo: mayor strike para un Bull Call Spread
        (long call + short call mas OTM), menor strike para un Bear Put
        Spread (long put + short put mas OTM).

        `cfg`: config de la instancia (self.cfg) que llama a este metodo; si
        se omite (ej. uso directo en tests), cae a SETTINGS.long_first. Antes
        este metodo ignoraba la config de la instancia y siempre leia
        SETTINGS.long_first global, lo cual rompia cualquier override pasado
        al constructor de WeeklyAsymmetricStrategy (ej. en tests).

        `max_quote_age_seconds`/`now` (BUG REAL CORREGIDO, ver
        RiskConfig.max_option_quote_staleness_seconds): si se pasan, una
        base candidata a "wing" cuyo book ya supere ese umbral de antiguedad
        se descarta de la busqueda - no se completa un spread comprometiendo
        una pata corta contra una cotizacion vieja (cadena de opciones caida
        sola mientras el resto del ciclo sigue fresco, ver docstring de
        scan_spread_completion_signals). Default None = sin filtro, mismo
        comportamiento que antes de esta guardia.
        """
        cfg = cfg if cfg is not None else SETTINGS.long_first
        min_wing_strike_diff = long_quote.strike * cfg.spread_wing_moneyness_pct
        same_series = [
            q for q in option_chain.all_quotes()
            if q.option_type is long_quote.option_type
            and q.expiry == long_quote.expiry
            and q.symbol != long_quote.symbol
            and (max_quote_age_seconds is None or not q.book.is_stale(max_quote_age_seconds, now=now))
        ]
        if long_quote.option_type is OptionType.CALL:
            wings = [q for q in same_series if q.strike >= long_quote.strike + min_wing_strike_diff]
            return min(wings, key=lambda q: q.strike) if wings else None
        wings = [q for q in same_series if q.strike <= long_quote.strike - min_wing_strike_diff]
        return max(wings, key=lambda q: q.strike) if wings else None

    @staticmethod
    def _trend_has_reversed(
        option_type: Optional[str], trend_at_entry: Optional[str], current_trend: str,
    ) -> bool:
        """
        MEJORA 2026-09-17 (ver config.LongFirstConfig.
        enable_trend_reversal_exit para la motivacion completa).
        Deliberadamente ESTRICTA: solo True cuando `current_trend` paso al
        EXTREMO CONTRARIO del que motivo la entrada -

            CALL comprada bajo BULLISH -> current_trend == BEARISH
            PUT  comprada bajo BEARISH -> current_trend == BULLISH

        Una lectura NEUTRAL de por medio (fading, no reversion confirmada
        al extremo contrario) NO cuenta como reversion - evita cerrar una
        posicion sana ante ruido de corto plazo del filtro tecnico (el
        mismo criterio conservador que ya rige en scan_entry_signals: bajo
        NEUTRAL no hay descarte direccional automatico). Una posicion sin
        `option_type`/`trend_at_entry` poblados (None, ver Position;
        incluida CUALQUIER posicion abierta antes de que estos dos campos
        existieran) o abierta bajo una tendencia de entrada que ya era
        NEUTRAL (sin tesis direccional de la cual "reversar") nunca
        dispara esta salida.
        """
        if option_type is None or trend_at_entry is None:
            return False
        if option_type == OptionType.CALL.value:
            return trend_at_entry == Trend.BULLISH.value and current_trend == Trend.BEARISH.value
        if option_type == OptionType.PUT.value:
            return trend_at_entry == Trend.BEARISH.value and current_trend == Trend.BULLISH.value
        return False

    # -- Salidas: glue hacia RiskManager.evaluate_position_exit() ---------------

    def build_exit_signals(
        self, portfolio: Portfolio, current_prices: Dict[str, float], now: datetime,
        current_greeks: Optional[Dict[str, Dict[str, float]]] = None,
        strategy_tag: str = "weekly_asymmetric",
        trend: str = Trend.NEUTRAL.value,
    ) -> List[ExitSignal]:
        """
        `current_prices`: mid vigente por simbolo (ej. desde el
        option_chain actual). `now`: datetime tz-aware inyectado por el
        llamador (nunca datetime.now() interno) para que este metodo sea
        testeable de forma determinista. `current_greeks`: griegas vigentes
        por simbolo (ej. `{q.symbol: q.greeks for q in option_chain.all_quotes()}`),
        opcional - solo hace falta para la salida por compresion de vega
        (ver risk_manager.evaluate_vega_decay_exit); sin este argumento, esa
        salida simplemente no se evalua (comportamiento identico al de
        antes de agregarla).

        `strategy_tag` (ver portfolio.Position.strategy_tag y el modo
        Scalping ADITIVO en config.ScalpingConfig): este metodo UNICAMENTE
        evalua/cierra posiciones marcadas con este tag (una posicion sin
        marca cuenta como "weekly_asymmetric") - nunca toca una posicion
        abierta por el modo Scalping (que tiene su propio
        ScalpingStrategy.build_exit_signals, con reglas de salida
        completamente distintas: minutos en vez de dias habiles, cierre
        EOD, reversion de IV). Default "weekly_asymmetric" preserva el
        comportamiento previo a este parametro para cualquier llamador que
        no lo pase - incluida la posicion de Octubre en produccion, que no
        tiene esta marca poblada.

        `trend` (MEJORA 2026-09-17, ver config.LongFirstConfig.
        enable_trend_reversal_exit y _trend_has_reversed mas abajo):
        lectura VIGENTE de tendencia 1D, misma inyectada que en
        scan_entry_signals()/scan_expensive_iv_spread_signals - se compara
        contra Position.trend_at_entry (congelado al fill) para decidir si
        la tesis direccional que motivo la entrada ya se invalidio. Default
        NEUTRAL preserva el comportamiento previo a este parametro para
        cualquier llamador que no lo pase (ademas, con
        enable_trend_reversal_exit apagado por defecto, esta salida ni
        siquiera se evalua).
        """
        cfg = self.cfg
        signals: List[ExitSignal] = []
        for position in portfolio.positions:
            if (position.strategy_tag or "weekly_asymmetric") != strategy_tag:
                continue  # posicion de OTRA estrategia (ej. Scalping) - no se toca aca
            if position.quantity <= 0:
                continue  # long-only: no hay pata corta propia que gestionar aca
            if position.entry_price is None or position.entry_time is None or position.expiry is None:
                continue  # posicion sin metadata de entrada: no se puede evaluar Stop Loss/Take Profit

            current_price = current_prices.get(position.symbol)
            reason = self.risk_manager.evaluate_position_exit(
                entry_price=position.entry_price, current_price=current_price,
                entry_time=position.entry_time, now=now, expiry=position.expiry,
                stop_loss_pct=cfg.stop_loss_pct, take_profit_pct=cfg.take_profit_pct,
                max_holding_business_days=cfg.max_holding_business_days,
                weekend_theta_guard_enabled=cfg.weekend_theta_guard_enabled,
                weekend_theta_guard_max_holding_business_days=cfg.weekend_theta_guard_max_holding_business_days,
                enable_tiered_stop_loss=cfg.enable_tiered_stop_loss,
                tiered_stop_loss_stage2_business_day=cfg.tiered_stop_loss_stage2_business_day,
                tiered_stop_loss_stage2_pct=cfg.tiered_stop_loss_stage2_pct,
                tiered_stop_loss_stage3_business_day=cfg.tiered_stop_loss_stage3_business_day,
                tiered_stop_loss_stage3_pct=cfg.tiered_stop_loss_stage3_pct,
            )

            # Salida por compresion de vega (complementa, no reemplaza, lo
            # de arriba): solo se evalua si nada disparo todavia y si el
            # llamador paso griegas vigentes. entry_vega viene de
            # Position.greeks_per_unit, congelado al momento del fill (ver
            # portfolio/portfolio.py) - nunca se actualiza despues, por eso
            # sirve de linea de base fija contra la cual medir la
            # compresion.
            if reason is None and cfg.enable_vega_decay_exit and current_greeks is not None:
                entry_vega = (position.greeks_per_unit or {}).get("vega")
                current_vega = (current_greeks.get(position.symbol) or {}).get("vega")
                reason = self.risk_manager.evaluate_vega_decay_exit(
                    entry_vega=entry_vega, current_vega=current_vega,
                    decay_ratio_threshold=cfg.vega_decay_exit_ratio,
                    entry_time=position.entry_time, now=now,
                    min_holding_hours=cfg.vega_decay_min_holding_hours,
                )

            # Salida por reversion de tendencia (MEJORA 2026-09-17, ver
            # config.LongFirstConfig.enable_trend_reversal_exit y
            # _trend_has_reversed): solo se evalua si nada disparo todavia
            # (misma prioridad que la salida por compresion de vega) y solo
            # si la posicion tiene la metadata de entrada necesaria
            # (Position.option_type/trend_at_entry - ninguna posicion
            # abierta antes de este campo la tiene, por lo que esta salida
            # nunca las afecta retroactivamente).
            if (
                reason is None
                and getattr(cfg, "enable_trend_reversal_exit", False)
                and self._trend_has_reversed(position.option_type, position.trend_at_entry, trend)
            ):
                reason = "trend_reversal_exit"

            if reason is not None:
                signals.append(ExitSignal(symbol=position.symbol, reason=reason, quantity=position.quantity))
                continue

            # Toma de ganancia parcial (complementa, no reemplaza, lo de
            # arriba): solo se evalua si NADA disparo un cierre total este
            # ciclo (Stop Loss/Take Profit/horizonte/guardia de fin de
            # semana/compresion de vega tienen prioridad), si todavia no se
            # tomo ganancia parcial antes para esta posicion, y si hay al
            # menos 2 contratos (con 1 solo no hay fraccion posible que deje
            # un "runner" - ver risk_manager.evaluate_partial_profit_take).
            if (
                cfg.enable_partial_profit_take
                and not position.partial_profit_taken
                and position.quantity >= 2
            ):
                should_take = self.risk_manager.evaluate_partial_profit_take(
                    entry_price=position.entry_price, current_price=current_price,
                    already_taken=position.partial_profit_taken,
                    trigger_pct=cfg.partial_profit_trigger_pct,
                )
                if should_take:
                    partial_qty = math.floor(position.quantity * cfg.partial_profit_take_fraction)
                    if 0 < partial_qty < position.quantity:
                        signals.append(ExitSignal(
                            symbol=position.symbol, reason="partial_profit_take", quantity=partial_qty,
                        ))
        return signals
