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
