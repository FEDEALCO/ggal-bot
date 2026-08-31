"""
implied_vol.py
==============
Solver de volatilidad implicita: invierte Black-Scholes a partir de un
precio de mercado (tipicamente el mid-price bid/ask). Estrategia:
Newton-Raphson primero (rapido cuando vega no es casi nula); si no converge
o sigma sale de un rango razonable, cae a biseccion (mas lento pero siempre
converge si existe solucion dentro del intervalo). Ver docs de diseño,
seccion 2.3, para la justificacion de este fallback.
"""

from __future__ import annotations

from typing import Optional

from .black_scholes import BlackScholesGreeks


class ImpliedVolatilityCalculator:
    def __init__(
        self,
        max_iter_newton: int = 50,
        tol: float = 1e-6,
        sigma_min: float = 1e-4,
        sigma_max: float = 5.0,
        max_iter_bisect: int = 100,
    ):
        self.max_iter_newton = max_iter_newton
        self.tol = tol
        self.sigma_min = sigma_min
        self.sigma_max = sigma_max
        self.max_iter_bisect = max_iter_bisect

    def solve(self, bs: BlackScholesGreeks, market_price: float, sigma_guess: float = 0.35) -> Optional[float]:
        sigma = self._newton_raphson(bs, market_price, sigma_guess)
        if sigma is not None and self.sigma_min < sigma < self.sigma_max:
            return sigma
        return self._bisection(bs, market_price)

    def _newton_raphson(
        self, bs: BlackScholesGreeks, market_price: float, sigma_guess: float
    ) -> Optional[float]:
        sigma = sigma_guess
        for _ in range(self.max_iter_newton):
            try:
                price = bs.price(sigma)
                vega = bs.vega(sigma)
            except ValueError:
                return None
            diff = price - market_price
            if abs(diff) < self.tol:
                return sigma
            if vega < 1e-8:
                return None  # vega casi nula -> Newton-Raphson inestable, usar biseccion
            sigma = sigma - diff / vega
            if sigma <= 0:
                return None
        return None

    def _bisection(self, bs: BlackScholesGreeks, market_price: float) -> Optional[float]:
        lo, hi = self.sigma_min, self.sigma_max
        try:
            f_lo = bs.price(lo) - market_price
            f_hi = bs.price(hi) - market_price
        except ValueError:
            return None
        if f_lo * f_hi > 0:
            return None  # no hay cambio de signo: precio fuera de rango arbitrable
        for _ in range(self.max_iter_bisect):
            mid = (lo + hi) / 2.0
            f_mid = bs.price(mid) - market_price
            if abs(f_mid) < self.tol:
                return mid
            if f_lo * f_mid < 0:
                hi = mid
            else:
                lo, f_lo = mid, f_mid
        return (lo + hi) / 2.0
