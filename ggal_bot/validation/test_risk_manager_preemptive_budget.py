"""
test_risk_manager_preemptive_budget.py
=========================================
Tests para risk/risk_manager.py::RiskManager.projected_greeks_breach
(MEJORA 2026-09-28: presupuesto preventivo de Griegas - ver
config.RiskConfig.enable_preemptive_greeks_budget). ADITIVO al chequeo de
limite duro ya existente (should_halt_new_positions): proyecta
current_totals + added_greeks de la señal que se esta por sizear ANTES de
comprometer capital, en vez de solo descubrir la violacion recien en el
proximo ciclo cuando ya se abrio la posicion.

Correr con:
    python -m ggal_bot.validation.test_risk_manager_preemptive_budget
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.risk.risk_manager import RiskLimits, RiskManager


def _manager(max_vega_total=5000.0, max_gamma_total=2000.0) -> RiskManager:
    return RiskManager(RiskLimits(max_vega_total=max_vega_total, max_gamma_total=max_gamma_total))


def test_projected_greeks_breach_none_within_budget():
    rm = _manager(max_vega_total=5000.0, max_gamma_total=2000.0)
    # proyectado vega=4000 (80% del limite duro), budget_fraction=0.85 -> limite efectivo=4250: OK.
    breach = rm.projected_greeks_breach(
        current_totals={"vega": 3000.0, "gamma": 500.0},
        added_greeks={"vega": 1000.0, "gamma": 100.0},
        budget_fraction=0.85,
    )
    assert breach is None


def test_projected_greeks_breach_on_vega():
    rm = _manager(max_vega_total=5000.0, max_gamma_total=2000.0)
    # proyectado vega=4500, budget_fraction=0.85 -> limite efectivo=4250: excede.
    breach = rm.projected_greeks_breach(
        current_totals={"vega": 3500.0, "gamma": 500.0},
        added_greeks={"vega": 1000.0, "gamma": 100.0},
        budget_fraction=0.85,
    )
    assert breach is not None
    assert "vega" in breach


def test_projected_greeks_breach_on_gamma():
    rm = _manager(max_vega_total=5000.0, max_gamma_total=2000.0)
    # proyectado gamma=1900, budget_fraction=0.85 -> limite efectivo=1700: excede.
    breach = rm.projected_greeks_breach(
        current_totals={"vega": 100.0, "gamma": 1500.0},
        added_greeks={"vega": 10.0, "gamma": 400.0},
        budget_fraction=0.85,
    )
    assert breach is not None
    assert "gamma" in breach


def test_projected_greeks_breach_reports_both_when_both_exceeded():
    rm = _manager(max_vega_total=5000.0, max_gamma_total=2000.0)
    breach = rm.projected_greeks_breach(
        current_totals={"vega": 4600.0, "gamma": 1900.0},
        added_greeks={"vega": 500.0, "gamma": 300.0},
        budget_fraction=0.85,
    )
    assert breach is not None
    assert "vega" in breach and "gamma" in breach


def test_projected_greeks_breach_budget_fraction_affects_threshold():
    """El mismo total proyectado no rompe con un budget_fraction mas laxo (mas cercano a 1.0)."""
    rm = _manager(max_vega_total=5000.0, max_gamma_total=2000.0)
    totals = {"vega": 3500.0, "gamma": 500.0}
    added = {"vega": 1000.0, "gamma": 100.0}
    # proyectado vega=4500: rompe con 0.85 (limite efectivo 4250)...
    assert rm.projected_greeks_breach(totals, added, budget_fraction=0.85) is not None
    # ...pero no con 1.0 (limite efectivo = limite duro = 5000).
    assert rm.projected_greeks_breach(totals, added, budget_fraction=1.0) is None


def test_projected_greeks_breach_handles_missing_keys_as_zero():
    rm = _manager(max_vega_total=5000.0, max_gamma_total=2000.0)
    breach = rm.projected_greeks_breach(
        current_totals={}, added_greeks={}, budget_fraction=0.85,
    )
    assert breach is None


def test_projected_greeks_breach_is_sign_agnostic():
    """Griegas netas negativas (posicion short-vega/gamma neta, si existiera) tambien deben
    proyectarse en valor absoluto - el limite es sobre la EXPOSICION, no el signo."""
    rm = _manager(max_vega_total=5000.0, max_gamma_total=2000.0)
    breach = rm.projected_greeks_breach(
        current_totals={"vega": -3500.0, "gamma": -500.0},
        added_greeks={"vega": -1000.0, "gamma": -100.0},
        budget_fraction=0.85,
    )
    assert breach is not None
    assert "vega" in breach


def test_projected_greeks_breach_is_additive_to_hard_limit_check():
    """should_halt_new_positions (limite duro, ya existente) sigue funcionando exactamente
    igual, sin que la nueva funcion lo reemplace ni lo modifique."""
    rm = _manager(max_vega_total=5000.0, max_gamma_total=2000.0)
    # Totales YA actuales por debajo del limite duro: should_halt_new_positions no debe frenar.
    assert rm.should_halt_new_positions({"vega": 4000.0, "gamma": 1000.0}) is False
    # El presupuesto PREVENTIVO si puede detectar el problema ANTES de que el total actual
    # (sin la nueva posicion) rompa el limite duro.
    breach = rm.projected_greeks_breach(
        current_totals={"vega": 4000.0, "gamma": 1000.0},
        added_greeks={"vega": 800.0, "gamma": 100.0},
        budget_fraction=0.85,
    )
    assert breach is not None


ALL_TESTS = [
    test_projected_greeks_breach_none_within_budget,
    test_projected_greeks_breach_on_vega,
    test_projected_greeks_breach_on_gamma,
    test_projected_greeks_breach_reports_both_when_both_exceeded,
    test_projected_greeks_breach_budget_fraction_affects_threshold,
    test_projected_greeks_breach_handles_missing_keys_as_zero,
    test_projected_greeks_breach_is_sign_agnostic,
    test_projected_greeks_breach_is_additive_to_hard_limit_check,
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
