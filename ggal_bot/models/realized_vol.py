"""
realized_vol.py
=================
Estimadores de volatilidad realizada a partir de cierres diarios (MEJORA
2026-09-28, a pedido explicito del usuario: "mejor trader quant... exprime
tu capacidad al maximo"). Funciones PURAS (sin I/O, sin estado), mismo
criterio que models/volatility_surface.py y models/black_scholes.py, para
que sean trivialmente testeables con series sinteticas deterministicas.

MOTIVACION: el GGAL en pesos tiene saltos discretos por eventos de
devaluacion/CCL que no son volatilidad en el sentido de un proceso de
difusion continua, son shocks puntuales. `close_to_close_realized_vol` (la
varianza clasica de retornos logaritmicos) trata un salto igual que
cualquier otro retorno - lo eleva al cuadrado y lo promedia con el resto,
asi que un solo dia de devaluacion fuerte "infla" el estimador durante toda
la ventana rodante siguiente, aunque el resto de los dias hayan sido
tranquilos. `bipower_realized_vol` (Barndorff-Nielsen & Shephard 2004) es
jump-robust POR CONSTRUCCION: en vez de sum(r_i^2), usa sum(|r_i| * |r_i-1|)
- multiplica cada retorno por su VECINO ADYACENTE en vez de por si mismo.
Un salto aislado (un solo dia con |r| enorme, vecinos normales) contribuye
una sola vez multiplicado por un vecino chico, en vez de aparecer al
cuadrado - queda fuertemente amortiguado en vez de dominar el estimador.
Si el salto se sostiene varios dias seguidos (un cambio de regimen real,
no un shock puntual), SI se refleja como vol alta genuina - la robustez es
especificamente a saltos AISLADOS de un dia, no a un cambio de regimen
sostenido, que es exactamente la distincion que se busca.

Ninguna de las dos funciones "sabe" cual usar - eso lo decide el llamador
(ver config.TechnicalAnalysisConfig.enable_jump_robust_hv y
data/technical_analysis.py::TechnicalAnalysisEngine.hv_estimate).
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

_DEFAULT_TRADING_DAYS_PER_YEAR = 252.0


def _log_returns(closes: Sequence[float]) -> List[float]:
    returns: List[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev is None or cur is None or prev <= 0 or cur <= 0:
            continue
        returns.append(math.log(cur / prev))
    return returns


def close_to_close_realized_vol(
    closes: Sequence[float],
    trading_days_per_year: float = _DEFAULT_TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """
    Estimador clasico (no robusto a saltos): desvio estandar de los retornos
    logaritmicos diarios, anualizado. None si no hay al menos 2 retornos
    validos (3 cierres). Se deja disponible sobre todo como referencia/
    comparacion para bipower_realized_vol de abajo - no es la que usa
    TechnicalAnalysisEngine.hv_estimate() cuando enable_jump_robust_hv=True.
    """
    returns = _log_returns(closes)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance * trading_days_per_year)


def bipower_realized_vol(
    closes: Sequence[float],
    trading_days_per_year: float = _DEFAULT_TRADING_DAYS_PER_YEAR,
) -> Optional[float]:
    """
    Bipower Variation (Barndorff-Nielsen & Shephard), jump-robust - ver
    docstring del modulo. None si no hay al menos 3 retornos validos (4
    cierres): se necesitan pares de retornos ADYACENTES, un retorno menos
    que close_to_close_realized_vol para el mismo largo de serie.

    BV = (pi/2) * promedio(|r_i| * |r_{i-1}|), i=2..n

    El factor (pi/2) corrige el sesgo de E[|Z|]=sqrt(2/pi) para Z~N(0,1)
    estandar (mismo factor que la literatura de bipower variation de
    Barndorff-Nielsen & Shephard), promediado sobre los (n-1) pares de
    retornos adyacentes disponibles.
    """
    returns = _log_returns(closes)
    n = len(returns)
    if n < 3:
        return None
    num_pairs = n - 1
    pairs_sum = sum(abs(returns[i]) * abs(returns[i - 1]) for i in range(1, n))
    bv_daily_variance = (math.pi / 2.0) * (pairs_sum / num_pairs)
    if bv_daily_variance < 0:
        return None
    return math.sqrt(bv_daily_variance * trading_days_per_year)
