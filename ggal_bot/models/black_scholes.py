"""
black_scholes.py
================
Pricer Black-Scholes con ajuste de tasa local (ARS) y convencion de dias,
y calculo de las griegas (delta, gamma, vega, theta, rho).

Nota sobre convencion de dias en BYMA: el tiempo a vencimiento para el
componente de "drift"/descuento (t_rate) se calcula en dias corridos / 365,
que es la convencion habitual de tasas en pesos (caucion/badlar). El tiempo
a vencimiento para el componente de riesgo (t_vol, que alimenta vega/gamma/theta)
se calcula en dias habiles / 252, porque la varianza se acumula con las ruedas
de operatoria, no con el paso del calendario. Separarlos evita distorsionar
las griegas alrededor de fines de semana largos o feriados, tipicos del
calendario argentino.

Las opciones de BYMA son de tipo americano; este pricer usa Black-Scholes
europeo como aproximacion base (valido para calls sin dividendos relevantes
en el horizonte, y como referencia conservadora para puts). El riesgo de
ejercicio anticipado se trata por fuera, en risk/risk_manager.py, no aca.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Tuple


def _norm_cdf(x: float) -> float:
    """CDF de la normal estandar via erf de la libreria estandar (sin scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


class OptionType(Enum):
    CALL = "call"
    PUT = "put"


@dataclass
class BlackScholesGreeks:
    spot: float                 # precio contado de GGAL
    strike: float
    rate: float                 # tasa libre de riesgo local anualizada (ej. caucion), decimal
    dividend_yield: float = 0.0
    days_calendar: int = 30     # dias corridos al vencimiento (descuento/forward)
    days_business: int = 21     # dias habiles al vencimiento (riesgo/vol)
    option_type: OptionType = OptionType.CALL

    @property
    def t_rate(self) -> float:
        return max(self.days_calendar, 0) / 365.0

    @property
    def t_vol(self) -> float:
        return max(self.days_business, 0) / 252.0

    def _d1_d2(self, sigma: float) -> Tuple[float, float]:
        if sigma <= 0 or self.t_vol <= 0:
            raise ValueError("sigma y t_vol deben ser positivos para calcular d1/d2")
        S, K, r, q, t_r, t_v = (
            self.spot, self.strike, self.rate, self.dividend_yield,
            self.t_rate, self.t_vol,
        )
        d1 = (math.log(S / K) + (r - q) * t_r + 0.5 * sigma * sigma * t_v) / (
            sigma * math.sqrt(t_v)
        )
        d2 = d1 - sigma * math.sqrt(t_v)
        return d1, d2

    def price(self, sigma: float) -> float:
        S, K, r, q, t_r = self.spot, self.strike, self.rate, self.dividend_yield, self.t_rate
        d1, d2 = self._d1_d2(sigma)
        disc_r = math.exp(-r * t_r)
        disc_q = math.exp(-q * t_r)
        if self.option_type is OptionType.CALL:
            return S * disc_q * _norm_cdf(d1) - K * disc_r * _norm_cdf(d2)
        return K * disc_r * _norm_cdf(-d2) - S * disc_q * _norm_cdf(-d1)

    def vega(self, sigma: float) -> float:
        """Vega por 1.00 (100 vol points) de cambio en sigma; ver all_greeks() para 'por punto'."""
        S, q, t_r, t_v = self.spot, self.dividend_yield, self.t_rate, self.t_vol
        d1, _ = self._d1_d2(sigma)
        return S * math.exp(-q * t_r) * _norm_pdf(d1) * math.sqrt(t_v)

    def delta(self, sigma: float) -> float:
        q, t_r = self.dividend_yield, self.t_rate
        d1, _ = self._d1_d2(sigma)
        disc_q = math.exp(-q * t_r)
        if self.option_type is OptionType.CALL:
            return disc_q * _norm_cdf(d1)
        return disc_q * (_norm_cdf(d1) - 1.0)

    def gamma(self, sigma: float) -> float:
        S, q, t_r, t_v = self.spot, self.dividend_yield, self.t_rate, self.t_vol
        d1, _ = self._d1_d2(sigma)
        return math.exp(-q * t_r) * _norm_pdf(d1) / (S * sigma * math.sqrt(t_v))

    def theta(self, sigma: float) -> float:
        """
        Theta diario, signo negativo tipico de posicion larga.

        BUG REAL CORREGIDO (encontrado en auditoria del 2026-08-27, ver
        docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.1): esta funcion sumaba
        `term1` (decaimiento por volatilidad, derivado respecto de `t_vol` =
        dias_habiles/252) con `term2+term3` (carry de tasa/dividendo, derivado
        respecto de `t_rate` = dias_calendario/365) y dividia la SUMA completa
        por 252 - mezclando los dos "relojes" de dias que el resto del pricer
        mantiene deliberadamente separados (ver docstring del modulo). Cada
        termino es la derivada parcial respecto de SU PROPIO tiempo (-dprice/dt),
        asi que cada uno debe escalarse a "por dia" con su propio day-count:
        term1 (t_vol) por 252, term2+term3 (t_rate) por 365. Verificado
        numericamente contra diferencia finita real (spot=strike=5200,
        r=0.40, sigma=0.55, 30 dias corridos/21 habiles): la formula vieja
        daba -11.93 (sobreestimaba la magnitud ~12%), la formula corregida
        da ~-10.56, que coincide con la diferencia finita (~-10.66). El error
        crecia con el peso relativo del carry (opciones mas largas, o con la
        tasa ARS tipicamente alta ~40% anual) y corrompia cualquier
        atribucion de PnL por griegas y la guardia de compresion de
        vega/weekend theta guard que consumen este numero.
        """
        S, K, r, q, t_r, t_v = (
            self.spot, self.strike, self.rate, self.dividend_yield,
            self.t_rate, self.t_vol,
        )
        d1, d2 = self._d1_d2(sigma)
        disc_r = math.exp(-r * t_r)
        disc_q = math.exp(-q * t_r)
        term_vol = -(S * disc_q * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(t_v))
        if self.option_type is OptionType.CALL:
            term_rate = -r * K * disc_r * _norm_cdf(d2)
            term_dividend = q * S * disc_q * _norm_cdf(d1)
        else:
            term_rate = r * K * disc_r * _norm_cdf(-d2)
            term_dividend = -q * S * disc_q * _norm_cdf(-d1)
        return term_vol / 252.0 + (term_rate + term_dividend) / 365.0

    def rho(self, sigma: float) -> float:
        K, r, t_r = self.strike, self.rate, self.t_rate
        _, d2 = self._d1_d2(sigma)
        disc_r = math.exp(-r * t_r)
        if self.option_type is OptionType.CALL:
            return K * t_r * disc_r * _norm_cdf(d2)
        return -K * t_r * disc_r * _norm_cdf(-d2)

    def all_greeks(self, sigma: float) -> Dict[str, float]:
        return {
            "price": self.price(sigma),
            "delta": self.delta(sigma),
            "gamma": self.gamma(sigma),
            "vega": self.vega(sigma) / 100.0,   # por punto de vol (1 vol point = 0.01)
            "theta": self.theta(sigma),
            "rho": self.rho(sigma) / 100.0,
        }
