"""
volatility_surface.py
======================
Construye, para un vencimiento dado, la curva de IV cruda por strike y la
suaviza con un ajuste cuadratico simple (proxy liviano de un SVI). Con la
curva suavizada se mide cuanto se aparta cada base individual de "lo que
deberia valer" segun sus vecinas (arbitraje de smile), separado de si el
nivel general de IV del vencimiento esta caro/barato respecto de la
volatilidad historica (arbitraje de nivel). Ver docs de diseño, seccion 1.1.
"""

from __future__ import annotations

import math
import statistics
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from ggal_bot.data.option_chain import OptionQuote


class VolatilitySurface:
    """
    BUG REAL CORREGIDO (auditoria del 2026-08-27, ver
    docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.2): el ajuste cuadratico
    tiene 3 parametros libres (a, b, c). Con exactamente 3 strikes validos
    (el minimo que exigia el codigo viejo), el "ajuste por minimos cuadrados"
    es una interpolacion PERFECTA - residuo cero en los 3 puntos por
    construccion, sin importar cuan mispriced este en realidad uno de ellos.
    Verificado numericamente: 3 strikes con IVs [0.55, 0.70, 0.55] (un salto
    deliberado de 15 vol points en el strike del medio) producian
    smile_dislocation() ~0 para los tres - la señal de arbitraje de smile,
    el corazon de la estrategia, quedaba matematicamente incapacitada para
    dispararse justo en el escenario de liquidez mas delgada (3 bases con
    book operable), que es el mas probable en la cadena de GGAL/BYMA.

    Fix: exigir materialmente mas puntos que grados de libertad antes de
    confiar en el residuo de la cuadratica (MIN_QUOTES_FOR_QUADRATIC = 5,
    deja al menos 2 grados de libertad de residuo). Con 3-4 puntos validos
    se cae a un ajuste LINEAL (IV = b*x + c, 2 parametros), que con 3 puntos
    ya deja 1 grado de libertad de residuo real - todavia captura el skew
    (pendiente), aunque no la curvatura completa del smile.
    """

    MIN_QUOTES_FOR_LINEAR = 3
    MIN_QUOTES_FOR_QUADRATIC = 5

    def __init__(self, quotes: List["OptionQuote"]):
        valid = [q for q in quotes if q.iv is not None]
        if len(valid) < self.MIN_QUOTES_FOR_LINEAR:
            raise ValueError(
                f"Se necesitan al menos {self.MIN_QUOTES_FOR_LINEAR} IVs validas para ajustar una curva"
            )
        self.quotes = valid
        if len(valid) >= self.MIN_QUOTES_FOR_QUADRATIC:
            self.fit_degree = 2
            self._fit_coeffs = self._fit_quadratic()
        else:
            self.fit_degree = 1
            b, c = self._fit_linear()
            self._fit_coeffs = (0.0, b, c)

    def _fit_linear(self) -> Tuple[float, float]:
        """Ajuste lineal IV = b*x + c, x = log(K/S), por minimos cuadrados (closed-form)."""
        xs = [math.log(q.strike / q.spot_ref) if getattr(q, "spot_ref", 0) else 0.0 for q in self.quotes]
        ys = [q.iv for q in self.quotes]
        n = len(xs)
        sx = sum(xs); sx2 = sum(x * x for x in xs); sy = sum(ys); sxy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sx2 - sx * sx
        if abs(denom) < 1e-12:
            return 0.0, statistics.mean(ys)  # degenerado (todos los strikes con el mismo x): curva plana
        b = (n * sxy - sx * sy) / denom
        c = (sy - b * sx) / n
        return b, c

    def _fit_quadratic(self) -> Tuple[float, float, float]:
        """Ajuste cuadratico IV = a*x^2 + b*x + c, x = log(K/S), por minimos cuadrados.

        Nota: en produccion x deberia calcularse como log(K/Forward), no
        log(K/mid de la opcion); aca se aproxima usando el spot de referencia
        de cada quote (ver data/option_chain.py) para mantener el modulo
        autocontenido.
        """
        xs = [math.log(q.strike / q.spot_ref) if getattr(q, "spot_ref", 0) else 0.0 for q in self.quotes]
        ys = [q.iv for q in self.quotes]
        n = len(xs)
        sx = sum(xs); sx2 = sum(x * x for x in xs); sx3 = sum(x ** 3 for x in xs); sx4 = sum(x ** 4 for x in xs)
        sy = sum(ys); sxy = sum(x * y for x, y in zip(xs, ys)); sx2y = sum(x * x * y for x, y in zip(xs, ys))
        A = [[sx4, sx3, sx2], [sx3, sx2, sx], [sx2, sx, n]]
        B = [sx2y, sxy, sy]
        return self._solve_3x3(A, B)

    @staticmethod
    def _solve_3x3(A: List[List[float]], B: List[float]) -> Tuple[float, float, float]:
        """Eliminacion gaussiana con pivoteo parcial para el sistema 3x3 del ajuste."""
        M = [row[:] + [B[i]] for i, row in enumerate(A)]
        n = 3
        for col in range(n):
            pivot_row = max(range(col, n), key=lambda r: abs(M[r][col]))
            M[col], M[pivot_row] = M[pivot_row], M[col]
            if abs(M[col][col]) < 1e-12:
                return (0.0, 0.0, statistics.mean(B))  # degenerado: devolver curva plana
            for r in range(n):
                if r != col:
                    factor = M[r][col] / M[col][col]
                    for c in range(col, n + 1):
                        M[r][c] -= factor * M[col][c]
        return tuple(M[i][n] / M[i][i] for i in range(n))  # type: ignore

    def smoothed_iv(self, quote: "OptionQuote") -> float:
        a, b, c = self._fit_coeffs
        x = math.log(quote.strike / quote.spot_ref) if getattr(quote, "spot_ref", 0) else 0.0
        return a * x * x + b * x + c

    def smile_dislocation(self, quote: "OptionQuote") -> float:
        """IV cruda - IV de curva, en vol points (positivo = base 'cara' relativa al smile)."""
        return (quote.iv - self.smoothed_iv(quote)) * 100.0

    def level_dislocation(self, hv_estimate: float) -> float:
        """Nivel promedio de IV del vencimiento vs. HV de referencia, en vol points."""
        avg_iv = statistics.mean(q.iv for q in self.quotes)
        return (avg_iv - hv_estimate) * 100.0
