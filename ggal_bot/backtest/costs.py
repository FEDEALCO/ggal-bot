"""
costs.py
=========
Modelo de costos REALES de operar opciones sobre acciones en BYMA a traves
de InvertirOnline (IOL), para poder llevar el PnL "a mid" que registran los
exports de shadow trading (ver dashboard/pnl_engine.py, ShadowAuditLogger:
los fills simulados se ejecutan EXACTAMENTE al precio de referencia/mid, sin
spread ni slippage) a un PnL NETO realista, tal como pidio el usuario para
la Fase 0 del backtest ("costos BYMA reales... spread cruzado, no mid").

FUENTES (relevadas por busqueda web el 2026-09-29, ver conversacion):
    1. Comision de bróker (IOL): tarifario publico en
       https://www.invertironline.com/tarifas - tres escalas segun volumen
       operado el mes anterior:
           Gold      ($0 - $7.500.000):        0.50%
           Platinum  ($7.500.001 - $50.000.000): 0.30%
           Black     ($50.000.001 en adelante):  0.10%
       Aplica sobre el monto de CADA operacion (compra y venta por
       separado), para acciones, bonos, CEDEARs, ONs Y OPCIONES por igual
       (el tarifario no discrimina un % distinto para opciones).
    2. Derecho de mercado BYMA: PDF publico "BYMA - Derechos de Mercado
       sobre Operaciones" (vigente desde 2026-06-09),
       https://cdn.prod.website-files.com/6697a441a50c6b926e1972e0/6a2875749a418f0439d7de8f_BYMA-Derechos-Mercado-sobre-Operaciones_2026-06-09.pdf
       Tabla de Opciones (transcripta via 3 fetches independientes del PDF,
       ver conversacion):
           Privados - CEDEAR - Opcion s/Prima:  0.2000%
           Privados - CEDEAR - Ejercicio:       0.0500%
           Publicos - Opcion s/Prima:           0.0600%
           Publicos - Ejercicio:                0.0100%
       AMBIGUEDAD EXPLICITA (no resuelta, ver DATA INSUFFICIENT abajo): el
       documento no tiene una fila separada "Privados - Acciones - Opcion
       s/Prima" distinta de "Privados - CEDEAR" (se transcribio 3 veces sin
       encontrarla). GGAL cotiza en BYMA como accion LOCAL, no como CEDEAR
       (el CEDEAR de GGAL es sobre el ADR de NYSE, un instrumento distinto
       de las opciones GFGC.../GFGV... que opera este bot). "Publicos" es
       claramente para bonos/titulos publicos, no aplica a una opcion sobre
       una accion privada como GGAL. Ante la ambiguedad, se usa el bucket
       "Privados" (0.20%) por ser el mas cercano estructuralmente (emisor
       privado) y el mas CONSERVADOR (mayor costo) de los dos aplicables a
       activos privados - un supuesto explicito, no un hecho verificado con
       el broker/BYMA directamente.
    3. IVA: 21% (alicuota general vigente en Argentina), segun el propio
       PDF de BYMA ("a los valores... se les debe agregar el IVA") y el
       tarifario de IOL ("para todas las comisiones aplica el cobro de
       IVA"). Ninguna de las dos fuentes citó el 21% explicitamente en el
       texto extraido: se usa la alicuota general de IVA de Argentina,
       vigente sin cambios conocidos a la fecha de este modulo.

DATA INSUFFICIENT (no fabricado, dejado como parametro explicito):
    No existe en ningun lado accesible un historico de bid/ask PUNTO A
    PUNTO al momento de cada fill pasado (los exports y el shadow log solo
    registran el precio de referencia/mid usado por el fill simulado). Por
    lo tanto el costo de CRUZAR EL SPREAD (comprar al ask, vender al bid)
    no se puede reconstruir con precision historica real para trades ya
    cerrados - se modela como una BANDA DE SENSIBILIDAD (ver
    SPREAD_SCENARIOS_PCT abajo) en vez de un numero unico presentado como
    medicion exacta. Los escenarios estan calibrados contra el propio
    umbral de diseño del bot (config.LongFirstConfig.execution_cost_max_pct,
    default 8% - ver ggal_bot/strategy/weekly_asymmetric.py:
    _estimate_execution_cost_pct), no contra una observacion de mercado en
    vivo (se intento leer el book en vivo de GGAL via la API del broker el
    2026-09-29 ~10:00 ART, pre-apertura de BYMA (10:30 ART): bid/ask
    vinieron `null` para toda la cadena, mercado no habia abierto).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

IVA_PCT = 0.21

# --- Comision de broker (IOL), por escala de volumen mensual operado ---
BROKER_COMMISSION_TIERS_PCT = {
    "gold": 0.0050,
    "platinum": 0.0030,
    "black": 0.0010,
}

# --- Derecho de mercado BYMA para opciones (ver AMBIGUEDAD en el docstring) ---
MARKET_RIGHTS_OPTIONS_PRIVADOS_PCT = 0.0020   # bucket usado (conservador) para GGAL
MARKET_RIGHTS_OPTIONS_PUBLICOS_PCT = 0.0006   # NO aplica a GGAL (bonos/titulos publicos) - referencia only

# --- Banda de sensibilidad para costo de cruce de spread (ver DATA INSUFFICIENT) ---
# Expresada como % del precio MID, aplicado en cada pata (entrada y salida)
# - es decir, el costo de "cruzar" en una sola pata es la MITAD de este
# numero (medio spread), y el round-trip completo (entrada + salida) paga
# el numero completo si ambas patas cruzan.
SPREAD_SCENARIOS_PCT: Tuple[float, ...] = (0.0, 0.03, 0.06, 0.10)


@dataclass(frozen=True)
class CostAssumptions:
    """
    Un escenario de costos completo, para poder correr el backtest bajo
    varios supuestos a la vez.

    `commission_pct_override`: si se pasa (no None), REEMPLAZA la lectura
    de `commission_tier` contra BROKER_COMMISSION_TIERS_PCT. Pensado para
    dos casos legitimos: (a) el usuario tiene una comision negociada
    distinta de las 3 escalas publicas del tarifario, o (b) un escenario
    explicito de "costo cero" (`0.0`) para aislar el PnL bruto "a mid" como
    punto de referencia - NUNCA un valor por defecto silencioso: si se usa,
    queda expuesto en el label/reporte de ese escenario.
    """
    commission_tier: str = "gold"
    commission_pct_override: Optional[float] = None
    market_rights_pct: float = MARKET_RIGHTS_OPTIONS_PRIVADOS_PCT
    iva_pct: float = IVA_PCT
    spread_round_trip_pct: float = 0.0  # 0.0 = "a mid" (igual al log historico), sin costo de spread

    @property
    def commission_pct(self) -> float:
        if self.commission_pct_override is not None:
            return self.commission_pct_override
        return BROKER_COMMISSION_TIERS_PCT[self.commission_tier]

    @property
    def regulatory_pct_per_leg(self) -> float:
        """(comision + derecho de mercado) * (1 + IVA), aplicado sobre el monto de CADA pata (entrada, salida)."""
        return (self.commission_pct + self.market_rights_pct) * (1.0 + self.iva_pct)


def round_trip_regulatory_cost_ars(notional_entry_ars: float, notional_exit_ars: float, assumptions: CostAssumptions) -> float:
    """
    Comision + derecho de mercado + IVA de AMBAS patas (entrada y salida),
    en pesos. Se calcula sobre el monto NOCIONAL de cada pata por separado
    (precio * cantidad * multiplicador), porque el precio (y por lo tanto
    el nocional) cambia entre entrada y salida.
    """
    rate = assumptions.regulatory_pct_per_leg
    return notional_entry_ars * rate + notional_exit_ars * rate


def spread_cost_ars(notional_entry_ars: float, notional_exit_ars: float, assumptions: CostAssumptions) -> float:
    """
    Costo de cruzar el spread en ambas patas, bajo el supuesto de sensibilidad
    `assumptions.spread_round_trip_pct` (ver DATA INSUFFICIENT en el
    docstring del modulo). Se reparte mitad y mitad entre entrada y salida
    (medio spread por pata), aplicado sobre el nocional de cada pata.
    """
    half = assumptions.spread_round_trip_pct / 2.0
    return notional_entry_ars * half + notional_exit_ars * half


def net_pnl_ars(
    pnl_gross_ars: float,
    notional_entry_ars: float,
    notional_exit_ars: float,
    assumptions: CostAssumptions,
) -> float:
    """
    PnL NETO de un trade cerrado, descontando comision+derechos+IVA (ambas
    patas) y el costo de spread asumido (ver DATA INSUFFICIENT). El PnL
    bruto de entrada (`pnl_gross_ars`) ya viene calculado "a mid" por el
    propio bot (ver dashboard/pnl_engine.py::match_trades_fifo o
    portfolio/lifecycle.py::build_episode_lifecycles) - este costo se le
    resta encima, nunca se recalcula el PnL bruto desde cero.
    """
    total_cost = (
        round_trip_regulatory_cost_ars(notional_entry_ars, notional_exit_ars, assumptions)
        + spread_cost_ars(notional_entry_ars, notional_exit_ars, assumptions)
    )
    return pnl_gross_ars - total_cost


def total_cost_ars(notional_entry_ars: float, notional_exit_ars: float, assumptions: CostAssumptions) -> float:
    return (
        round_trip_regulatory_cost_ars(notional_entry_ars, notional_exit_ars, assumptions)
        + spread_cost_ars(notional_entry_ars, notional_exit_ars, assumptions)
    )


def default_scenarios() -> Tuple[CostAssumptions, ...]:
    """
    Escenarios estandar para la Fase 0: comision Gold (la mas conservadora
    de las 3 escalas, salvo que el usuario confirme un volumen mensual que
    lo ubique en Platinum/Black) + derecho de mercado "Privados" (ver
    AMBIGUEDAD), con la banda completa de supuestos de spread.
    """
    return tuple(
        CostAssumptions(spread_round_trip_pct=s) for s in SPREAD_SCENARIOS_PCT
    )


def commission_tier_scenarios(spread_round_trip_pct: float = 0.0) -> Tuple[CostAssumptions, ...]:
    """
    Un escenario por cada escala de comision de IOL (Gold/Platinum/Black),
    a un supuesto de spread FIJO (por defecto 0.0 = "mid_sin_spread", para
    aislar el efecto de la comision del efecto del spread). Pensado para
    responder "cuanto cambia el resultado si mi cuenta califica para una
    escala de comision distinta a Gold" sin mezclar ese eje con el de
    sensibilidad de spread (ver default_scenarios).

    IMPORTANTE: la escala real de la cuenta del usuario todavia NO fue
    confirmada (depende de su volumen mensual operado - DATA INSUFFICIENT,
    pendiente de que el usuario la provea). Estos 3 escenarios son una
    banda de sensibilidad, no una eleccion de cual aplica en la practica.
    """
    return tuple(
        CostAssumptions(commission_tier=tier, spread_round_trip_pct=spread_round_trip_pct)
        for tier in ("gold", "platinum", "black")
    )
