"""
test_realized_vol.py
======================
Tests para models/realized_vol.py (MEJORA 2026-09-28: estimador de
volatilidad realizada robusto a saltos - ver docstring de ese modulo y
config.TechnicalAnalysisConfig.enable_jump_robust_hv).

Correr con:
    python -m ggal_bot.validation.test_realized_vol
"""
from __future__ import annotations

import math
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.models.realized_vol import bipower_realized_vol, close_to_close_realized_vol


def _geometric_series(daily_returns):
    closes = [1000.0]
    for r in daily_returns:
        closes.append(closes[-1] * math.exp(r))
    return closes


def test_close_to_close_none_with_fewer_than_three_closes():
    assert close_to_close_realized_vol([1000.0, 1010.0]) is None


def test_bipower_none_with_fewer_than_four_closes():
    # bipower necesita 3 retornos (4 cierres) - con solo 2 retornos (3 cierres) debe ser None.
    assert bipower_realized_vol([1000.0, 1010.0, 1005.0]) is None


def test_both_estimators_agree_closely_on_a_quiet_series_without_jumps():
    # Serie determinista sin saltos: ambos estimadores deben coincidir en orden de magnitud.
    returns = [0.01, -0.01, 0.008, -0.012, 0.005, -0.006, 0.011, -0.009, 0.004, -0.003] * 3
    closes = _geometric_series(returns)
    cc = close_to_close_realized_vol(closes)
    bv = bipower_realized_vol(closes)
    assert cc is not None and bv is not None
    assert cc > 0 and bv > 0
    # No deberian diferir mas de ~30% entre si sin ningun salto presente.
    assert abs(cc - bv) / cc < 0.30


def test_bipower_is_far_less_inflated_than_close_to_close_by_a_single_jump():
    """
    Tesis central de la mejora (ver docstring del modulo): un UNICO salto
    aislado (ej. devaluacion de un dia) infla close-to-close mucho mas que
    bipower, que pondera el salto contra sus vecinos en vez de al cuadrado
    contra si mismo.
    """
    quiet_returns = [0.01, -0.008, 0.006, -0.011, 0.009, -0.007, 0.004, -0.005, 0.012, -0.01] * 3
    quiet_closes = _geometric_series(quiet_returns)
    cc_quiet = close_to_close_realized_vol(quiet_closes)
    bv_quiet = bipower_realized_vol(quiet_closes)

    jump_returns = list(quiet_returns)
    jump_returns[15] = 0.25  # +25% en un solo dia, en medio de la serie
    jump_closes = _geometric_series(jump_returns)
    cc_jump = close_to_close_realized_vol(jump_closes)
    bv_jump = bipower_realized_vol(jump_closes)

    cc_ratio = cc_jump / cc_quiet
    bv_ratio = bv_jump / bv_quiet
    assert cc_ratio > 1.5, "close-to-close deberia inflarse fuertemente con el salto"
    assert bv_ratio < cc_ratio, "bipower debe inflarse MENOS que close-to-close ante el mismo salto aislado"


def test_close_to_close_ignores_non_positive_closes():
    # Un cierre invalido (<=0) se salta al calcular retornos, no crashea ni fabrica un valor.
    result = close_to_close_realized_vol([1000.0, 0.0, 1010.0, 1005.0, 1020.0])
    assert result is None or result >= 0.0


ALL_TESTS = [
    test_close_to_close_none_with_fewer_than_three_closes,
    test_bipower_none_with_fewer_than_four_closes,
    test_both_estimators_agree_closely_on_a_quiet_series_without_jumps,
    test_bipower_is_far_less_inflated_than_close_to_close_by_a_single_jump,
    test_close_to_close_ignores_non_positive_closes,
]


if __name__ == "__main__":
    failures = 0
    for test_fn in ALL_TESTS:
        try:
            test_fn()
            print(f"OK   - {test_fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL - {test_fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR - {test_fn.__name__}: {exc!r}")

    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} tests OK")
    if failures:
        raise SystemExit(1)
