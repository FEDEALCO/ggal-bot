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
from datetime import datetime
from typing import Dict, List, Optional

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionChain, OptionQuote
from ggal_bot.data.technical_analysis import MomentumShift, Trend
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.models.microstructure import passes_obi_filter
from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.portfolio.portfolio import Portfolio
from ggal_bot.risk.risk_manager import RiskManager


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
class ExitSignal:
    symbol: str
    reason: str            # "stop_loss" | "take_profit" | "weekly_horizon_expired" | "weekend_theta_guard" | "vega_theta_decay"
    action: str = "sell_to_close"
    quantity: float = 0.0


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
    blocked_by_liquidity: int = 0
    blocked_by_obi: int = 0
    blocked_by_moneyness: int = 0
    evaluated_for_dislocation: int = 0  # llegaron al chequeo de smile (pasaron todos los filtros anteriores)
    blocked_by_dislocation: int = 0     # llegaron pero no alcanzaron el umbral vigente (normal o extremo bajo NEUTRAL)
    qualified: int = 0                  # generaron EntrySignal
    trend: str = ""
    closest_miss_symbol: Optional[str] = None
    closest_miss_dislocation: Optional[float] = None          # dislocation real observada (mas negativo = mas barata)
    closest_miss_threshold_required: Optional[float] = None   # -smile_threshold exigido para esa opcion puntual
    closest_miss_shortfall_vol_points: Optional[float] = None  # cuanto le falto en puntos de vol (siempre >= 0)


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
        """
        cfg = self.cfg
        ta_cfg = SETTINGS.technical_analysis

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

        def _smile_threshold_for(option_type: OptionType) -> Optional[float]:
            """
            Umbral de dislocacion de smile a exigir para `option_type` bajo
            la tendencia/momentum vigentes, o None si `option_type` debe
            descartarse de plano (bloqueo direccional estricto, sin
            reversion temprana que lo habilite).
            """
            if not ta_cfg.enabled:
                return normal_threshold  # filtro tecnico desactivado por config: comportamiento pre-modulo
            if trend == Trend.BULLISH.value:
                if option_type is OptionType.CALL:
                    return normal_threshold
                return extreme_threshold if momentum_override_type is option_type else None
            if trend == Trend.BEARISH.value:
                if option_type is OptionType.PUT:
                    return normal_threshold
                return extreme_threshold if momentum_override_type is option_type else None
            return extreme_threshold  # NEUTRAL

        diag = EntryScanDiagnostics(total_quotes=len(surface.quotes), trend=trend)

        candidates: List[EntrySignal] = []
        for q in surface.quotes:
            smile_threshold = _smile_threshold_for(q.option_type)
            if smile_threshold is None:
                diag.blocked_by_direction += 1
                continue  # filtro direccional tecnico: bajo BULLISH/BEARISH sin reversion temprana, ni se evalua

            # Horizonte semanal: nunca se abre una posicion que exceda el
            # maximo de ruedas habiles configurado, aunque este muy barata.
            if q.days_business > cfg.max_holding_business_days:
                diag.blocked_by_holding_days += 1
                continue

            volume = recent_volumes.get(q.symbol, 0.0)
            if not self.risk_manager.check_liquidity(q.book, volume):
                diag.blocked_by_liquidity += 1
                continue

            # Confirmacion de microestructura (ver models/microstructure.py):
            # filtro de CALIDAD DE EJECUCION, no de alpha direccional - evita
            # levantar la oferta justo cuando el libro muestra un desbalance
            # extremo hacia el lado vendedor (tipico de una punta
            # aislada/iliquida en un libro tan delgado como el de GGAL).
            if cfg.enable_obi_filter and not passes_obi_filter(q.book, cfg.min_obi_for_entry):
                diag.blocked_by_obi += 1
                continue

            if not q.spot_ref or q.spot_ref <= 0:
                continue
            log_moneyness = math.log(q.strike / q.spot_ref)
            if abs(log_moneyness) > cfg.moneyness_band_pct:
                diag.blocked_by_moneyness += 1
                continue  # fuera de la banda ATM/OTM cercana (convexidad objetivo)

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
                continue  # no esta "barata" (o no lo suficiente bajo NEUTRAL): NUNCA se genera señal de venta para abrir
            if not level_ok:
                continue

            premium = q.book.mid
            if premium <= 0:
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

        candidates.sort(key=lambda s: s.convexity_score, reverse=True)
        diag.qualified = len(candidates)
        self.last_scan_diagnostics = diag
        return candidates

    # -- Spreads: la pata corta SOLO si la larga ya esta confirmada en portafolio --

    def scan_spread_completion_signals(
        self, option_chain: OptionChain, portfolio: Portfolio, trend: str = Trend.NEUTRAL.value,
        max_quote_age_seconds: Optional[float] = None, now: Optional[float] = None,
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
            if allowed_option_type is not None and quote.option_type is not allowed_option_type:
                continue  # filtro direccional tecnico: no se agrega exposicion contraria a la tendencia vigente

            long_qty = self._confirmed_long_quantity(portfolio, quote.symbol)
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
    def _confirmed_long_quantity(portfolio: Portfolio, symbol: str) -> float:
        """Suma solo exposicion LARGA (quantity > 0) de `symbol` - nunca cuenta posiciones cortas."""
        return sum(p.quantity for p in portfolio.positions if p.symbol == symbol and p.quantity > 0)

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

    # -- Salidas: glue hacia RiskManager.evaluate_position_exit() ---------------

    def build_exit_signals(
        self, portfolio: Portfolio, current_prices: Dict[str, float], now: datetime,
        current_greeks: Optional[Dict[str, Dict[str, float]]] = None,
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
        """
        cfg = self.cfg
        signals: List[ExitSignal] = []
        for position in portfolio.positions:
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
                )

            if reason is not None:
                signals.append(ExitSignal(symbol=position.symbol, reason=reason, quantity=position.quantity))
        return signals
