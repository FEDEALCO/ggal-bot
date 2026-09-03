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
        """
        if entry_price is None or entry_price <= 0 or current_price is None:
            return None

        pnl_pct = (current_price - entry_price) / entry_price

        if pnl_pct <= -abs(stop_loss_pct):
            return "stop_loss"
        if pnl_pct >= abs(take_profit_pct):
            return "take_profit"

        holding_business_days = _business_days_between(entry_time.date(), now.date())
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
        """
        if entry_vega is None or current_vega is None or entry_vega == 0:
            return None
        ratio = abs(current_vega) / abs(entry_vega)
        if ratio <= decay_ratio_threshold:
            return "vega_theta_decay"
        return None

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
