"""
test_backtest_costs.py
=========================
Tests para ggal_bot/backtest/costs.py: modelo de costos reales de operar
opciones en BYMA via IOL (comision + derecho de mercado + IVA + banda de
sensibilidad de spread) usado en la Fase 0 del backtest.

Correr con:
    python -m ggal_bot.validation.test_backtest_costs
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.backtest.costs import (
    BROKER_COMMISSION_TIERS_PCT,
    CostAssumptions,
    commission_tier_scenarios,
    default_scenarios,
    net_pnl_ars,
    round_trip_regulatory_cost_ars,
    spread_cost_ars,
    total_cost_ars,
)


def test_regulatory_cost_matches_hand_computed_gold_tier():
    # Gold 0.50% + derecho de mercado 0.20% = 0.70%, x (1+0.21 IVA) = 0.847% por pata.
    assumptions = CostAssumptions(commission_tier="gold", spread_round_trip_pct=0.0)
    assert abs(assumptions.regulatory_pct_per_leg - 0.00847) < 1e-9

    entry_notional, exit_notional = 100_000.0, 105_000.0
    expected = entry_notional * 0.00847 + exit_notional * 0.00847
    assert abs(round_trip_regulatory_cost_ars(entry_notional, exit_notional, assumptions) - expected) < 1e-6


def test_regulatory_cost_scales_with_commission_tier():
    gold = CostAssumptions(commission_tier="gold")
    black = CostAssumptions(commission_tier="black")
    entry, exit_ = 100_000.0, 100_000.0
    cost_gold = round_trip_regulatory_cost_ars(entry, exit_, gold)
    cost_black = round_trip_regulatory_cost_ars(entry, exit_, black)
    # Black (0.10%) tiene una comision de broker mas baja que Gold (0.50%): costo total menor.
    assert cost_black < cost_gold


def test_spread_cost_zero_when_scenario_is_mid():
    assumptions = CostAssumptions(spread_round_trip_pct=0.0)
    assert spread_cost_ars(100_000.0, 100_000.0, assumptions) == 0.0


def test_spread_cost_splits_half_and_half_per_leg():
    # spread round-trip de 10% -> 5% por pata (medio spread cada una).
    assumptions = CostAssumptions(spread_round_trip_pct=0.10)
    entry, exit_ = 100_000.0, 90_000.0
    expected = entry * 0.05 + exit_ * 0.05
    assert abs(spread_cost_ars(entry, exit_, assumptions) - expected) < 1e-6


def test_net_pnl_subtracts_total_cost_from_gross():
    assumptions = CostAssumptions(commission_tier="gold", spread_round_trip_pct=0.06)
    gross = 10_000.0
    entry, exit_ = 100_000.0, 105_000.0
    cost = total_cost_ars(entry, exit_, assumptions)
    assert abs(net_pnl_ars(gross, entry, exit_, assumptions) - (gross - cost)) < 1e-6


def test_net_pnl_never_improves_on_gross():
    """Los costos NUNCA pueden mejorar el PnL bruto - deben ser siempre >= 0."""
    assumptions = CostAssumptions(commission_tier="gold", spread_round_trip_pct=0.10)
    for gross in (-50_000.0, 0.0, 50_000.0):
        net = net_pnl_ars(gross, 100_000.0, 100_000.0, assumptions)
        assert net <= gross + 1e-9


def test_default_scenarios_cover_the_full_spread_band():
    scenarios = default_scenarios()
    assert len(scenarios) == 4
    spreads = sorted(s.spread_round_trip_pct for s in scenarios)
    assert spreads == [0.0, 0.03, 0.06, 0.10]
    # Todos usan el mismo tier/derecho de mercado por defecto (solo varia el supuesto de spread).
    assert all(s.commission_tier == "gold" for s in scenarios)


def test_all_commission_tiers_are_positive_and_decreasing_with_volume():
    tiers = BROKER_COMMISSION_TIERS_PCT
    assert tiers["gold"] > tiers["platinum"] > tiers["black"] > 0.0


def test_commission_pct_override_replaces_tier_lookup():
    assumptions = CostAssumptions(commission_tier="gold", commission_pct_override=0.0)
    assert assumptions.commission_pct == 0.0
    # market_rights sigue aplicando salvo que tambien se ponga en 0 explicitamente.
    zero_all = CostAssumptions(commission_pct_override=0.0, market_rights_pct=0.0)
    assert zero_all.regulatory_pct_per_leg == 0.0
    assert round_trip_regulatory_cost_ars(100_000.0, 100_000.0, zero_all) == 0.0


def test_commission_tier_scenarios_covers_all_three_tiers_at_fixed_spread():
    scenarios = commission_tier_scenarios(spread_round_trip_pct=0.0)
    assert len(scenarios) == 3
    tiers = sorted(s.commission_tier for s in scenarios)
    assert tiers == ["black", "gold", "platinum"]
    # El eje de spread queda FIJO (no se mezcla con la sensibilidad de spread).
    assert all(s.spread_round_trip_pct == 0.0 for s in scenarios)


def test_commission_tier_scenarios_respects_custom_spread():
    scenarios = commission_tier_scenarios(spread_round_trip_pct=0.06)
    assert all(s.spread_round_trip_pct == 0.06 for s in scenarios)


ALL_TESTS = [
    test_regulatory_cost_matches_hand_computed_gold_tier,
    test_regulatory_cost_scales_with_commission_tier,
    test_spread_cost_zero_when_scenario_is_mid,
    test_spread_cost_splits_half_and_half_per_leg,
    test_net_pnl_subtracts_total_cost_from_gross,
    test_net_pnl_never_improves_on_gross,
    test_default_scenarios_cover_the_full_spread_band,
    test_all_commission_tiers_are_positive_and_decreasing_with_volume,
    test_commission_pct_override_replaces_tier_lookup,
    test_commission_tier_scenarios_covers_all_three_tiers_at_fixed_spread,
    test_commission_tier_scenarios_respects_custom_spread,
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
