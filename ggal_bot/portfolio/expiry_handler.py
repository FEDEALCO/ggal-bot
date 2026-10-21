"""
expiry_handler.py
===================
Cierre AUTOMATICO de Position vencidas - PRIORIDAD 1, 2026-10-21, bug de
riesgo permitido durante el freeze de 3 semanas (ver FREEZE.md: "solo bugs
de integridad de datos o riesgo").

HALLAZGO REAL que lo motiva (export de Northflank del 2026-10-21, ver
RESEARCH.md): las 8 posiciones que el bot consideraba "abiertas" en ese
momento eran, las 8, de la serie de opciones de Octubre - vencida el
2026-10-16, cinco dias antes del export. Nada en el codigo existente
cerraba una Position por vencimiento (ver VALID_EVENT_TYPES en
portfolio/event_journal.py antes de este cambio: ENTRY/ADD/REDUCE/
PARTIAL_EXIT/CLOSE/REJECT/CANCEL/SHADOW_RESET - ninguno disparado por el
simple paso del tiempo). Dos consecuencias reales, verificadas leyendo el
codigo (ver RiskConfig.enable_expiry_autoclose en config.py para el detalle
completo de ambas):
  1. Mientras el proceso corre sin restart: delta fantasma congelado desde
     el ultimo fill, sumado al chequeo de riesgo direccional agregado
     (RiskLimitsConfig.max_portfolio_delta_ars).
  2. Tras un restart: Portfolio.has_unknown_greeks()=True para estas
     posiciones (ya no estan en la cadena viva) -> bloqueo INCONDICIONAL
     de entradas nuevas (RiskManager.should_halt_new_positions), sin
     importar el delta.

QUE HACE ESTE MODULO: una funcion PURA (sin I/O de red, sin reloj implicito
salvo el que se le pasa) que recorre `portfolio.positions`, identifica las
que ya vencieron (`Position.expiry` ya paso, evaluado "al cierre del dia de
vencimiento" - ver market_hours.is_after_session_close) y para cada una:
  - calcula su valor intrinseco contra el spot de cierre pasado por el
    llamador (OTM -> 0.0; ITM -> intrinseco real, marcado explicitamente
    como "ejercicio supuesto" porque esto es shadow mode: nunca hubo un
    ejercicio real, se asume el resultado economico de haberlo hecho);
  - la cierra en el Position Lifecycle Event Journal con un evento EXPIRY
    (mismo trato que CLOSE para todo lector existente - ver
    portfolio/reconciliation.py y backtest/reconstruct.py, actualizados
    junto con este modulo);
  - pone su `quantity` en 0 (MISMO criterio que un CLOSE normal - ver
    Position.position_id: "los objetos Position con quantity=0 siguen en
    Portfolio.positions, nunca se remueven" - no se introduce un
    comportamiento nuevo de "remover de la lista"; `quantity=0` ya basta
    para que Position.contribution() aporte 0 a las 4 Griegas sin importar
    greeks_per_unit, sacandola EFECTIVAMENTE del calculo agregado).

NUNCA fabrica un strike o un tipo de opcion: si el `symbol` no matchea el
patron conocido de tickers GGAL (ver `parse_option_type_and_strike`), esa
posicion se deja SIN TOCAR y se reporta en `unparseable_symbols` del
resultado - mejor una posicion vieja detectable como "rara" que una
cerrada con un valor inventado.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Optional

from ggal_bot import market_hours
from ggal_bot.portfolio.event_journal import PositionEventJournal
from ggal_bot.portfolio.portfolio import Portfolio

logger = logging.getLogger("ggal_bot.portfolio.expiry_handler")

# MISMO patron ya validado en ggal_bot/backtest/attribution.py::parse_option_symbol
# ("GFGC7000OC" -> call/7000.0, "GFGV6400I" -> put/6400.0) - duplicado aca
# a proposito en vez de importar backtest/ desde el runtime del bot (capas
# separadas: backtest/ es para analisis offline, no una dependencia del
# bot en produccion). Si este patron cambia, actualizar AMBOS lugares (hay
# un test de regresion en validation/test_expiry_handler.py que compara
# los dos resultados sobre la misma lista de symbols para detectar drift).
_SYMBOL_RE = re.compile(r"^GFG([CV])(\d+)[A-Z]*$")


def parse_option_type_and_strike(symbol: str) -> Optional[tuple]:
    """Extrae (option_type, strike) de un ticker GGAL ("call"/"put", float).
    Devuelve None si el symbol no matchea el patron conocido - NUNCA fabrica
    un strike u option_type."""
    m = _SYMBOL_RE.match(symbol)
    if not m:
        return None
    option_type = "call" if m.group(1) == "C" else "put"
    strike = float(m.group(2))
    return option_type, strike


def intrinsic_value(option_type: str, strike: float, spot: float) -> float:
    """Valor intrinseco real de una opcion call/put contra `spot` - nunca
    negativo (una opcion jamas vale menos que 0 para su tenedor largo)."""
    if option_type == "call":
        return max(spot - strike, 0.0)
    if option_type == "put":
        return max(strike - spot, 0.0)
    raise ValueError(f"option_type desconocido: {option_type!r} (se esperaba 'call' o 'put')")


@dataclass
class ExpiryClosure:
    """Una Position vencida que esta funcion efectivamente cerro."""
    position_id: str
    symbol: str
    strategy_tag: str
    quantity_closed: float          # magnitud (siempre > 0) de lo cerrado
    was_long: bool                  # True si la Position tenia quantity>0 antes de cerrar
    option_type: str
    strike: float
    spot_at_expiry: float
    intrinsic_value: float
    itm: bool
    expiry: date


def close_expired_positions(
    portfolio: Portfolio,
    journal: PositionEventJournal,
    spot: float,
    now: Optional[datetime] = None,
) -> List[ExpiryClosure]:
    """
    Cierra, a valor intrinseco, toda Position de `portfolio` cuyo
    vencimiento ya paso "al cierre del dia de vencimiento" (ver
    market_hours.is_after_session_close: si `expiry` es HOY en ART, espera
    a que la rueda de hoy ya haya cerrado; si `expiry` es una fecha
    anterior, se cierra sin esperar nada mas).

    `spot`: spot de GGAL vigente al momento de evaluar este cierre (el
    llamador es responsable de pasar el spot de cierre real del dia, nunca
    se fabrica aca).
    `now`: inyectable para tests (ver resto del bot) - default
    datetime.now(timezone.utc) si se omite.

    Posiciones con quantity==0 (ya cerradas antes, por cualquier via) o sin
    `expiry` poblado se ignoran sin generar ningun evento (no hay nada que
    cerrar / no hay vencimiento real conocido - nunca se fabrica uno).
    Una Position vencida cuyo `symbol` no matchea el patron conocido de
    tickers de opciones GGAL (ver parse_option_type_and_strike) se deja SIN
    TOCAR (sigue abierta, sigue contribuyendo lo que ya contribuia) y se
    loguea un warning explicito - nunca se fabrica un strike/option_type
    para poder cerrarla igual.

    Devuelve la lista de `ExpiryClosure` (una por Position efectivamente
    cerrada esta llamada) - vacia si no habia nada vencido.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    today_art = market_hours.art_date(now)

    closures: List[ExpiryClosure] = []
    for pos in portfolio.positions:
        if pos.quantity == 0:
            continue
        if pos.expiry is None:
            continue
        if pos.expiry > today_art:
            continue  # todavia no vencio
        if pos.expiry == today_art and not market_hours.is_after_session_close(now):
            continue  # vence HOY pero la rueda de hoy no cerro todavia

        parsed = parse_option_type_and_strike(pos.symbol)
        if parsed is None:
            logger.warning(
                "expiry_handler: Position %s (symbol=%s, position_id=%s, expiry=%s) ya "
                "vencio pero su symbol no matchea el patron conocido de opciones GGAL - "
                "se deja SIN CERRAR (nunca se fabrica un strike/option_type).",
                pos.symbol, pos.symbol, pos.position_id, pos.expiry,
            )
            continue

        option_type, strike = parsed
        value = intrinsic_value(option_type, strike, spot)
        itm = value > 0.0
        was_long = pos.quantity > 0
        quantity_closed = abs(pos.quantity)
        strategy_tag = pos.strategy_tag or "weekly_asymmetric"

        reason = "expiry_itm_ejercicio_supuesto" if itm else "expiry_otm"
        side = "sell" if was_long else "buy"  # misma convencion que un CLOSE normal (ver event_journal callers)
        quantity_delta = -pos.quantity  # flattens a 0 sin importar el signo

        journal.log_event(
            "EXPIRY",
            position_id=pos.position_id,
            contract_key=pos.contract_key,
            symbol=pos.symbol,
            strategy_tag=strategy_tag,
            side=side,
            quantity_delta=quantity_delta,
            quantity_after=0.0,
            price=value,
            order_client_id="",
            reason=reason,
        )

        pos.quantity = 0.0

        closures.append(ExpiryClosure(
            position_id=pos.position_id, symbol=pos.symbol, strategy_tag=strategy_tag,
            quantity_closed=quantity_closed, was_long=was_long,
            option_type=option_type, strike=strike, spot_at_expiry=spot,
            intrinsic_value=value, itm=itm, expiry=pos.expiry,
        ))

        logger.warning(
            "expiry_handler: Position %s (symbol=%s, strategy_tag=%s, qty=%s, "
            "vencimiento=%s) cerrada por vencimiento a valor intrinseco $%.4f "
            "(%s, spot=$%.2f).",
            pos.position_id, pos.symbol, strategy_tag,
            "+" + str(quantity_closed) if was_long else "-" + str(quantity_closed),
            pos.expiry, value, "ITM - ejercicio supuesto" if itm else "OTM", spot,
        )

    return closures
