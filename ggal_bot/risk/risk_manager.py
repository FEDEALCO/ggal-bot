"""
risk_manager.py
===============
Limites de riesgo y filtros de liquidez, y punto central de decision sobre
si el bot puede seguir abriendo posiciones nuevas. Ver docs de diseño,
seccion 2.5, para la justificacion de cada limite (stop por descalce de
vega/gamma, filtros de liquidez minima, riesgo de ejercicio/garantias).

Los valores por defecto de RiskLimits deben calibrarse contra el tamaño
real de cuenta y la volatilidad reciente de GGAL; los que trae este modulo
son ilustrativos (ver config.RiskConfig para los valores efectivos usados
por run_bot.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

from ggal_bot.data.option_chain import OrderBookSnapshot


def _business_days_between(start: date, end: date) -> int:
    """
    Cuenta dias habiles (lunes a viernes, sin feriados locales) entre dos
    fechas. Duplicado deliberadamente de data/market_data_feed.py (misma
    logica) en vez de importarlo, para que risk/ no dependa de data/ - este
    modulo se mantiene con dependencias minimas a proposito.
    """
    if end <= start:
        return 0
    days = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


@dataclass
class RiskLimits:
    max_vega_total: float = 5000.0      # $ por punto de vol (1 vol point = 0.01 de IV)
    max_gamma_total: float = 2000.0     # $ por (punto de movimiento de GGAL)^2
    max_spread_relative: float = 0.05   # spread relativo maximo para considerar operable
    min_book_size: float = 20.0         # tamaño minimo en punta (contratos)
    min_daily_volume: float = 50.0      # volumen minimo operado reciente


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def check_liquidity(self, book: OrderBookSnapshot, recent_volume: float) -> bool:
        if recent_volume < self.limits.min_daily_volume:
            return False
        return book.is_tradeable(self.limits.max_spread_relative, self.limits.min_book_size)

    def check_greeks_limits(self, totals: Dict[str, float]) -> Dict[str, bool]:
        return {
            "vega_ok": abs(totals.get("vega", 0.0)) <= self.limits.max_vega_total,
            "gamma_ok": abs(totals.get("gamma", 0.0)) <= self.limits.max_gamma_total,
        }

    def should_halt_new_positions(self, totals: Dict[str, float]) -> bool:
        checks = self.check_greeks_limits(totals)
        return not all(checks.values())

    def breach_report(self, totals: Dict[str, float]) -> str:
        """Texto corto para logging/alertas cuando se excede algun limite."""
        checks = self.check_greeks_limits(totals)
        breaches = [name for name, ok in checks.items() if not ok]
        if not breaches:
            return "Griegas dentro de limite."
        return f"LIMITE EXCEDIDO: {', '.join(breaches)} | totales={totals}"

    # -----------------------------------------------------------------------
    # Modo Long-First / Weekly Asymmetric (ver config.LongFirstConfig y
    # strategy/weekly_asymmetric.py): Stop Loss / Take Profit / horizonte
    # semanal / guardia de fin de semana para posiciones LARGAS de opciones.
    # Unica fuente de verdad de "cuando forzar un cierre" - la estrategia
    # solo hace de glue entre esto y el portafolio (ver
    # WeeklyAsymmetricStrategy.build_exit_signals).
    # -----------------------------------------------------------------------

    def evaluate_position_exit(
        self,
        entry_price: float,
        current_price: Optional[float],
        entry_time: datetime,
        now: datetime,
        expiry: date,
        stop_loss_pct: float,
        take_profit_pct: float,
        max_holding_business_days: int,
        weekend_theta_guard_enabled: bool = True,
        enable_tiered_stop_loss: bool = False,
        tiered_stop_loss_stage2_business_day: int = 2,
        tiered_stop_loss_stage2_pct: float = 0.35,
        tiered_stop_loss_stage3_business_day: int = 4,
        tiered_stop_loss_stage3_pct: float = 0.20,
    ) -> Optional[str]:
        """
        Devuelve el motivo de cierre forzado ("stop_loss", "take_profit",
        "weekly_horizon_expired", "weekend_theta_guard") o None si la
        posicion no dispara ninguna regla todavia.

        Bajo el modo Long-First todas las posiciones evaluadas aca son
        LARGAS por construccion (comprar una opcion es la unica operacion
        de apertura permitida - ver strategy/weekly_asymmetric.py), asi que
        el PnL% se mide siempre como (precio_actual - precio_entrada) /
        precio_entrada sobre la PRIMA, sin necesidad de considerar el lado
        (largo/corto): una perdida de -stop_loss_pct% de la prima pagada es
        la perdida maxima que este modo tolera antes de cortar, en vez de
        dejar que la opcion siga perdiendo valor tiempo hasta el
        vencimiento.

        `enable_tiered_stop_loss` (MEJORA 2026-09-04, ver seguimiento del
        analisis del export de trades del 01-04/09/2026 - docs/ auditoria
        de esa fecha): angosta progresivamente el Stop Loss a medida que
        pasan los dias habiles desde la entrada, en vez de sostener el
        mismo -stop_loss_pct% fijo durante las 5 ruedas del horizonte
        semanal completo. Motivo real: ese export mostro 16 posiciones que
        quedaron abiertas de un dia para el otro, con perdidas de hasta
        -33.5% sin que el stop fijo de -50% llegara a frenarlas a tiempo -
        angostar el stop a medida que se acerca el limite del horizonte
        reduce esa cola de perdidas sin tocar el lado de ganancia (el mejor
        resultado observado en esa ventana fue apenas +8.82%, muy lejos del
        +100% de Take Profit). Los "stages" son ACUMULATIVOS por dias
        habiles mantenidos (no dias corridos):
            dias < tiered_stop_loss_stage2_business_day            -> stop_loss_pct tal cual (sin cambios)
            tiered_stop_loss_stage2_business_day <= dias < stage3   -> tiered_stop_loss_stage2_pct
            dias >= tiered_stop_loss_stage3_business_day            -> tiered_stop_loss_stage3_pct
        Se espera stage2_pct <= stop_loss_pct y stage3_pct <= stage2_pct
        (angostar, nunca ensanchar) pero esto NO se valida aca (ver
        config.LongFirstConfig) - un typo de config que ensanche el stop en
        vez de angostarlo no crashea, solo deja de cumplir el objetivo de
        la mejora. Default `enable_tiered_stop_loss=False`: backward-
        compatible para cualquier llamador que no lo pase (incluidos los
        tests existentes) - preserva el stop_loss_pct fijo de siempre.
        """
        if entry_price is None or entry_price <= 0 or current_price is None:
            return None

        pnl_pct = (current_price - entry_price) / entry_price
        holding_business_days = _business_days_between(entry_time.date(), now.date())

        effective_stop_loss_pct = abs(stop_loss_pct)
        if enable_tiered_stop_loss:
            if holding_business_days >= tiered_stop_loss_stage3_business_day:
                effective_stop_loss_pct = abs(tiered_stop_loss_stage3_pct)
            elif holding_business_days >= tiered_stop_loss_stage2_business_day:
                effective_stop_loss_pct = abs(tiered_stop_loss_stage2_pct)

        if pnl_pct <= -effective_stop_loss_pct:
            return "stop_loss"
        if pnl_pct >= abs(take_profit_pct):
            return "take_profit"

        if holding_business_days >= max_holding_business_days:
            return "weekly_horizon_expired"

        # Guardia de fin de semana: el fin de semana son 2-3 dias corridos
        # de theta sin ninguna rueda para reaccionar si la volatilidad
        # esperada no se materializo. Solo aplica si el vencimiento es
        # POSTERIOR a este viernes (si vence el viernes mismo, da lo mismo:
        # ya se va a resolver por vencimiento, no hace falta forzar nada).
        if weekend_theta_guard_enabled and now.weekday() == 4 and expiry > now.date():
            return "weekend_theta_guard"

        return None

    def evaluate_vega_decay_exit(
        self,
        entry_vega: Optional[float],
        current_vega: Optional[float],
        decay_ratio_threshold: float = 0.35,
        entry_time: Optional[datetime] = None,
        now: Optional[datetime] = None,
        min_holding_hours: float = 0.0,
    ) -> Optional[str]:
        """
        Complementa (no reemplaza) evaluate_position_exit(): la tesis de
        convexidad que motivo comprar una opcion barata es, precisamente,
        exposicion a vega/gamma - si el |vega| actual ya cayo por debajo de
        `decay_ratio_threshold` del |vega| que tenia al momento de la
        entrada (ver Position.greeks_per_unit, congelado al fill - nunca se
        actualiza despues), esa tesis ya se agoto: la opcion dejo de ser
        sensible a la volatilidad tanto como cuando se compro, tipicamente
        porque el tiempo al vencimiento se acorto y/o el subyacente se movio
        lejos del strike. Seguir sosteniendo la posicion a partir de ahi es
        pagar theta por una exposicion que ya no es la que se buscaba,
        aunque el PnL% de la prima todavia no dispare Stop Loss ni Take
        Profit.

        Devuelve "vega_theta_decay" o None. Con `entry_vega`/`current_vega`
        ausentes (None) o `entry_vega == 0` (no deberia pasar en la
        practica: toda opcion con greeks validos tiene vega != 0), no se
        evalua nada - la ausencia de informacion nunca fuerza un cierre.

        `entry_time`/`now`/`min_holding_hours` (FLEXIBILIZACION 2026-09-04,
        a pedido explicito del usuario: esta salida estaba generando muchos
        cierres con PnL bajo - el vega de una opcion ATM/cercana al dinero
        puede comprimirse muy rapido apenas el subyacente se mueve un poco
        en las primeras horas de vida de la posicion, sin que eso signifique
        todavia que la tesis de convexidad fallo de verdad). Si se pasan los
        tres y `min_holding_hours > 0`, esta salida NO se evalua hasta que la
        posicion lleve al menos `min_holding_hours` horas abierta - le da
        tiempo a la posicion a desarrollarse antes de cortarla por
        compresion de vega. Default `min_holding_hours=0.0` (con
        `entry_time`/`now` en None por default tambien): backward-compatible
        para cualquier llamador que no los pase (incluidos los tests
        existentes) - sin gate de tiempo, identico al comportamiento previo.
        """
        if entry_vega is None or current_vega is None or entry_vega == 0:
            return None
        if entry_time is not None and now is not None and min_holding_hours > 0:
            hours_held = (now - entry_time).total_seconds() / 3600.0
            if hours_held < min_holding_hours:
                return None
        ratio = abs(current_vega) / abs(entry_vega)
        if ratio <= decay_ratio_threshold:
            return "vega_theta_decay"
        return None

    def evaluate_partial_profit_take(
        self,
        entry_price: Optional[float],
        current_price: Optional[float],
        already_taken: bool,
        trigger_pct: float = 0.15,
    ) -> bool:
        """
        MEJORA 2026-09-04 (ver seguimiento del analisis del export de trades
        del 01-04/09/2026): complementa (no reemplaza) evaluate_position_exit()
        - se evalua UNICAMENTE si esa funcion (y evaluate_vega_decay_exit) no
        devolvieron ya un motivo de cierre TOTAL para el mismo ciclo (ver
        WeeklyAsymmetricStrategy.build_exit_signals, que respeta ese orden de
        prioridad). En esa ventana ninguna operacion llego a tocar el
        +100% de Take Profit (el mejor resultado individual fue +8.82%), asi
        que una parte de la ganancia no realizada terminaba dandose vuelta
        antes del cierre final - esta salida asegura una FRACCION de la
        posicion (ver config.LongFirstConfig.partial_profit_take_fraction)
        apenas el PnL% no realizado supera `trigger_pct`, dejando el resto
        como "runner" sujeto a las mismas reglas de siempre.

        Devuelve True si corresponde tomar ganancia parcial en este ciclo,
        False en cualquier otro caso (incluida la ausencia de datos). NO
        calcula la cantidad a vender ni marca nada como ya tomado - eso es
        responsabilidad del llamador (ver Position.partial_profit_taken,
        poblado recien cuando el fill de venta parcial se confirma, nunca
        antes, en run_bot.py:_act_on_exit_signal).

        `already_taken`: si ya se tomo ganancia parcial antes para esta
        misma posicion (Position.partial_profit_taken), esta salida no
        vuelve a dispararse - se toma UNA sola vez por posicion, nunca en
        cada ciclo mientras el PnL% siga por encima del umbral (eso vaciaria
        el "runner" de a poco en vez de una unica vez).
        """
        if already_taken:
            return False
        if entry_price is None or entry_price <= 0 or current_price is None:
            return False
        pnl_pct = (current_price - entry_price) / entry_price
        return pnl_pct >= abs(trigger_pct)

    # -----------------------------------------------------------------------
    # Modo Scalping Intradia (ver config.ScalpingConfig y
    # strategy/scalping.py:ScalpingStrategy) - modulo ADITIVO, ver la nota
    # de arquitectura en config.py junto a ScalpingConfig. Equivalente de
    # evaluate_position_exit()/evaluate_vega_decay_exit() de arriba, pero
    # para posiciones de scalping: mismas dos primeras reglas (Stop
    # Loss/Take Profit sobre la PRIMA), horizonte de holding en MINUTOS (no
    # dias habiles), un cierre preventivo por FALTA DE PROGRESO, y un
    # cierre OBLIGATORIO de Fin de Dia (EOD) en horario de Argentina -
    # ninguna posicion de scalping se sostiene de un dia para el otro.
    # -----------------------------------------------------------------------

    def evaluate_scalping_exit(
        self,
        entry_price: Optional[float],
        current_price: Optional[float],
        entry_time: datetime,
        now: datetime,
        expiry: date,  # noqa: ARG002 - se recibe por simetria con evaluate_position_exit; el horizonte de scalping no depende del vencimiento (ver max_holding_minutes)
        stop_loss_pct: float,
        take_profit_pct: float,
        max_holding_minutes: float,
        min_progress_pnl_pct: float,
        progress_check_minutes: float,
        eod_close_enabled: bool = True,
        eod_close_time: str = "16:50",
        eod_timezone_offset_hours: float = -3.0,
    ) -> Optional[str]:
        """
        Devuelve "scalping_stop_loss" | "scalping_take_profit" |
        "scalping_horizon_expired" | "scalping_no_progress" |
        "scalping_eod_close", o None si ninguna regla dispara todavia.

        Orden de evaluacion: Stop Loss/Take Profit primero (mismo criterio
        de PnL% sobre la prima que evaluate_position_exit), despues
        horizonte acelerado (`max_holding_minutes`) y falta de progreso
        (`min_progress_pnl_pct` no alcanzado a los `progress_check_minutes`
        de abierta - "moverse rapido o salir", la tesis central de
        scalping), y por ultimo el cierre EOD. El cierre EOD es el UNICO
        que se evalua incluso sin `current_price` disponible (es una
        guardia de HORARIO, no de PnL - no tiene sentido dejar de forzarlo
        solo porque no hay una cotizacion fresca en este instante).
        """
        if entry_price is None or entry_price <= 0 or current_price is None:
            if eod_close_enabled and self._is_past_eod(now, eod_close_time, eod_timezone_offset_hours):
                return "scalping_eod_close"
            return None

        pnl_pct = (current_price - entry_price) / entry_price

        if pnl_pct <= -abs(stop_loss_pct):
            return "scalping_stop_loss"
        if pnl_pct >= abs(take_profit_pct):
            return "scalping_take_profit"

        minutes_held = (now - entry_time).total_seconds() / 60.0
        if minutes_held >= max_holding_minutes:
            return "scalping_horizon_expired"
        if minutes_held >= progress_check_minutes and pnl_pct < min_progress_pnl_pct:
            return "scalping_no_progress"

        if eod_close_enabled and self._is_past_eod(now, eod_close_time, eod_timezone_offset_hours):
            return "scalping_eod_close"

        return None

    @staticmethod
    def _is_past_eod(now: datetime, eod_close_time: str, tz_offset_hours: float) -> bool:
        """
        True si `now` (tz-aware, tipicamente UTC) ya paso la hora de cierre
        configurada EN HORARIO DE ARGENTINA (ver ScalpingConfig.
        eod_timezone_offset_hours, default -3.0 = ART todo el año, sin
        horario de verano desde 2009 - se usa un offset fijo simple en vez
        de zoneinfo/pytz a proposito, solo para esto no vale agregar esa
        dependencia). Formato de `eod_close_time`: "HH:MM" (24hs). Un
        formato invalido se trata de forma conservadora como "todavia no
        paso el cierre" (un typo de config nunca debe forzar un cierre
        inesperado).
        """
        try:
            hour_str, minute_str = eod_close_time.split(":")
            eod_hour, eod_minute = int(hour_str), int(minute_str)
        except (ValueError, AttributeError):
            return False
        local_now = now.astimezone(timezone(timedelta(hours=tz_offset_hours)))
        return (local_now.hour, local_now.minute) >= (eod_hour, eod_minute)
