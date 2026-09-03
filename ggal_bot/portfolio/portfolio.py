"""
portfolio.py
============
Representacion de posiciones (subyacente y opciones) y agregacion de
griegas totales de la cuenta, en total y desagregadas por vencimiento
(critico para vega: la vega del mes corriente no es intercambiable con
la del mes siguiente, ver docs de diseño seccion 1.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional


@dataclass
class Position:
    symbol: str
    quantity: float             # + largo, - corto; en contratos (multiplicador aparte)
    multiplier: float           # 100 para opciones en BYMA, 1 para acciones
    greeks_per_unit: Optional[Dict[str, float]] = None  # None para el subyacente (delta=1)
    expiry: Optional[date] = None
    # Metadata de entrada (opcional, default None por compatibilidad hacia
    # atras): la usa risk.risk_manager.RiskManager.evaluate_position_exit()
    # para decidir Stop Loss / Take Profit / horizonte semanal / guardia de
    # fin de semana en el modo Long-First (ver strategy/weekly_asymmetric.py
    # y config.LongFirstConfig). Sin estos dos campos poblados, esa
    # evaluacion simplemente se omite para la posicion (ver
    # WeeklyAsymmetricStrategy.build_exit_signals).
    entry_price: Optional[float] = None
    entry_time: Optional[datetime] = None

    # Marca de que ESTRATEGIA abrio esta posicion (ver config.ScalpingConfig
    # y strategy/scalping.py): None (default, compatibilidad hacia atras)
    # se trata como "weekly_asymmetric" en todos los puntos que filtran por
    # esta marca (ver strategy/weekly_asymmetric.py:build_exit_signals/
    # _confirmed_long_quantity y run_bot.py:_capital_available_ars) - asi,
    # ninguna posicion abierta ANTES de que este campo existiera (incluida
    # la posicion de Octubre en produccion) cambia de dueño por este
    # agregado. El modo Scalping (aditivo, ver config.ScalpingConfig) marca
    # sus propias posiciones con "scalping" para mantenerlas completamente
    # aisladas de weekly_asymmetric/vol_arbitrage: cada estrategia evalua y
    # cierra UNICAMENTE las posiciones con su propia marca (o sin marca,
    # para "weekly_asymmetric"), nunca las de otra.
    strategy_tag: Optional[str] = None

    def contribution(self) -> Dict[str, float]:
        qty_mult = self.quantity * self.multiplier
        if self.greeks_per_unit is None:
            return {"delta": qty_mult, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        g = self.greeks_per_unit
        return {
            "delta": qty_mult * g.get("delta", 0.0),
            "gamma": qty_mult * g.get("gamma", 0.0),
            "vega": qty_mult * g.get("vega", 0.0),
            "theta": qty_mult * g.get("theta", 0.0),
        }


@dataclass
class Portfolio:
    positions: List[Position] = field(default_factory=list)

    def add(self, position: Position) -> None:
        self.positions.append(position)

    def clear(self) -> None:
        self.positions.clear()

    def total_greeks(self) -> Dict[str, float]:
        totals = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        for pos in self.positions:
            c = pos.contribution()
            for k in totals:
                totals[k] += c[k]
        return totals

    def greeks_by_expiry(self) -> Dict[Optional[date], Dict[str, float]]:
        out: Dict[Optional[date], Dict[str, float]] = {}
        for pos in self.positions:
            c = pos.contribution()
            bucket = out.setdefault(pos.expiry, {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0})
            for k in bucket:
                bucket[k] += c[k]
        return out

    def greeks_for_strategy_tag(self, strategy_tag: str) -> Dict[str, float]:
        """
        Griegas totales de SOLO las posiciones marcadas con `strategy_tag`
        (ver Position.strategy_tag y config.ScalpingConfig) - contraparte de
        `total_greeks()` (que suma TODA la cartera, sin distinguir
        estrategia) usada por run_bot.py:_greeks_for_strategy() para darle a
        cada estrategia (weekly_asymmetric/vol_arbitrage vs. scalping) su
        propio techo de riesgo de Griegas, aislado, de forma simetrica a
        como ya funciona el capital (`_capital_available_ars`).

        Una posicion sin marca (`strategy_tag is None`, toda posicion
        abierta antes de que este campo existiera, incluida cualquier
        posicion viva de produccion previa a este cambio) cuenta como
        "weekly_asymmetric", preservando el comportamiento exacto de antes
        de este metodo para ese caso.
        """
        totals = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
        for pos in self.positions:
            if (pos.strategy_tag or "weekly_asymmetric") != strategy_tag:
                continue
            c = pos.contribution()
            for k in totals:
                totals[k] += c[k]
        return totals
