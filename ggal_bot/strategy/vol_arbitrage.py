"""
vol_arbitrage.py
=================
Orquesta la deteccion de descalibres de volatilidad (IV cruda vs. curva
suavizada del vencimiento) y arma señales de compra/venta, aplicando
primero el filtro de liquidez del RiskManager. Ver docs de diseño,
seccion 1.1 y 2.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.risk.risk_manager import RiskManager


@dataclass
class TradeSignal:
    symbol: str
    action: str              # "buy" o "sell"
    reason: str
    iv_dislocation_vol_points: float


class VolatilityArbitrageStrategy:
    def __init__(self, risk_manager: RiskManager, smile_threshold_vol_points: float = 3.0):
        self.risk_manager = risk_manager
        self.smile_threshold_vol_points = smile_threshold_vol_points

    def scan_for_signals(
        self,
        surface: VolatilitySurface,
        recent_volumes: Dict[str, float],
    ) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        for q in surface.quotes:
            volume = recent_volumes.get(q.symbol, 0.0)
            if not self.risk_manager.check_liquidity(q.book, volume):
                continue
            dislocation = surface.smile_dislocation(q)
            if dislocation > self.smile_threshold_vol_points:
                signals.append(TradeSignal(q.symbol, "sell", "IV cruda por encima de la curva (cara)", dislocation))
            elif dislocation < -self.smile_threshold_vol_points:
                signals.append(TradeSignal(q.symbol, "buy", "IV cruda por debajo de la curva (barata)", dislocation))
        return signals
