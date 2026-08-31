"""
historical_volatility.py
========================
Estimadores de volatilidad historica realizada sobre GGAL: close-to-close
(simple, ruidoso) y Parkinson (usa maximo/minimo intradiario, mas eficiente).
Ver docs de diseño, seccion 1.1, para el rol de la HV como ancla frente a la IV.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, List, Tuple


class HistoricalVolatility:
    @staticmethod
    def close_to_close(closes: List[float], annualization_days: int = 252) -> float:
        if len(closes) < 2:
            raise ValueError("Se necesitan al menos 2 precios de cierre")
        log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        if len(log_returns) < 2:
            raise ValueError("Se necesitan al menos 2 retornos para el desvio")
        daily_std = statistics.stdev(log_returns)
        return daily_std * math.sqrt(annualization_days)

    @staticmethod
    def parkinson(highs: List[float], lows: List[float], annualization_days: int = 252) -> float:
        if len(highs) != len(lows) or len(highs) == 0:
            raise ValueError("highs y lows deben tener la misma longitud y no estar vacios")
        factor = 1.0 / (4.0 * math.log(2.0))
        sq_terms = [factor * (math.log(h / l)) ** 2 for h, l in zip(highs, lows)]
        mean_sq = sum(sq_terms) / len(sq_terms)
        return math.sqrt(mean_sq * annualization_days)

    @staticmethod
    def multi_window(closes: List[float], windows: Tuple[int, ...] = (5, 10, 20, 60)) -> Dict[int, float]:
        out = {}
        for w in windows:
            if len(closes) > w:
                out[w] = HistoricalVolatility.close_to_close(closes[-(w + 1):])
        return out
