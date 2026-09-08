"""
test_position_lifecycle.py
=============================
Tests del Position Lifecycle MINIMO (TANDA 2 "OPTIMIZACION EJECUTABLE",
seccion 4, 2026-09-08) - ggal_bot/portfolio/lifecycle.py.build_episode_lifecycles.
No requiere un CSV real en disco: opera sobre listas de dicts con el mismo
schema que PositionEventJournal._HEADER (ver ese modulo), tal como las
devolveria pandas.read_csv(...).to_dict("records") en produccion.
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.portfolio.lifecycle import build_episode_lifecycles


def _row(ts, event_type, position_id, symbol="GFGC5200O", strategy_tag="weekly_asymmetric",
         side="", quantity_delta=None, quantity_after=None, price=None, reason=""):
    return {
        "timestamp_utc": ts, "event_type": event_type, "position_id": position_id,
        "contract_key": "", "symbol": symbol, "strategy_tag": strategy_tag, "side": side,
        "quantity_delta": quantity_delta, "quantity_after": quantity_after,
        "price": price, "order_client_id": "", "reason": reason, "data_unavailable_fields": "",
    }


def test_simple_entry_and_close_produces_one_closed_episode():
    events = [
        _row("2026-09-01T14:00:00+00:00", "ENTRY", "p1", side="buy",
             quantity_delta=10, quantity_after=10, price=100.0, reason="smile_dislocation"),
        _row("2026-09-02T15:00:00+00:00", "CLOSE", "p1", side="sell",
             quantity_delta=-10, quantity_after=0, price=120.0, reason="take_profit"),
    ]
    episodes = build_episode_lifecycles(events)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.position_id == "p1"
    assert ep.episode_id == "p1"
    assert ep.strategy == "weekly_asymmetric"
    assert ep.symbol == "GFGC5200O"
    assert ep.initial_quantity == 10
    assert ep.current_quantity == 0
    assert ep.average_entry == 100.0
    assert ep.close_reason == "take_profit"
    assert not ep.is_open
    assert ep.realized_pnl == (120.0 - 100.0) * 10 * 100.0  # multiplicador de opciones
    assert ep.data_insufficient_fields == []


def test_entry_partial_exit_and_close_accumulates_realized_pnl_across_reductions():
    events = [
        _row("2026-09-01T14:00:00+00:00", "ENTRY", "p2", side="buy",
             quantity_delta=10, quantity_after=10, price=100.0, reason="smile_dislocation"),
        _row("2026-09-02T14:00:00+00:00", "PARTIAL_EXIT", "p2", side="sell",
             quantity_delta=-5, quantity_after=5, price=115.0, reason="partial_profit_take"),
        _row("2026-09-03T14:00:00+00:00", "CLOSE", "p2", side="sell",
             quantity_delta=-5, quantity_after=0, price=90.0, reason="stop_loss"),
    ]
    episodes = build_episode_lifecycles(events)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.current_quantity == 0
    assert ep.close_reason == "stop_loss"
    expected_pnl = (115.0 - 100.0) * 5 * 100.0 + (90.0 - 100.0) * 5 * 100.0
    assert ep.realized_pnl == expected_pnl


def test_open_position_without_close_event_has_no_closed_at_or_close_reason():
    events = [
        _row("2026-09-05T14:00:00+00:00", "ENTRY", "p3", side="buy",
             quantity_delta=6, quantity_after=6, price=80.0, reason="smile_dislocation"),
    ]
    episodes = build_episode_lifecycles(events)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.is_open
    assert ep.closed_at is None
    assert ep.close_reason is None
    assert ep.current_quantity == 6
    assert ep.realized_pnl == 0.0
    # Todavia no hay salida - realized_pnl=0.0 es CORRECTO (no faltan datos,
    # simplemente no se realizo nada todavia), no debe marcarse insuficiente.
    assert "realized_pnl" not in ep.data_insufficient_fields


def test_events_without_position_id_are_ignored_reject_and_cancel():
    events = [
        _row("2026-09-01T14:00:00+00:00", "REJECT", "", reason="kill_switch_tripped"),
        _row("2026-09-01T14:05:00+00:00", "ENTRY", "p4", side="buy",
             quantity_delta=3, quantity_after=3, price=50.0, reason="smile_dislocation"),
    ]
    episodes = build_episode_lifecycles(events)
    assert len(episodes) == 1
    assert episodes[0].position_id == "p4"


def test_multiple_position_ids_produce_independent_episodes_in_first_seen_order():
    events = [
        _row("2026-09-01T14:00:00+00:00", "ENTRY", "p5", symbol="GFGC5200O",
             quantity_delta=1, quantity_after=1, price=100.0),
        _row("2026-09-01T15:00:00+00:00", "ENTRY", "p6", symbol="GFGV4800O",
             quantity_delta=2, quantity_after=2, price=60.0),
        _row("2026-09-02T15:00:00+00:00", "CLOSE", "p5", symbol="GFGC5200O",
             quantity_delta=-1, quantity_after=0, price=110.0, reason="take_profit"),
    ]
    episodes = build_episode_lifecycles(events)
    assert [e.position_id for e in episodes] == ["p5", "p6"]
    assert episodes[0].is_open is False
    assert episodes[1].is_open is True


def test_missing_entry_price_marks_average_entry_and_realized_pnl_as_data_insufficient():
    """
    Caso deliberadamente corrupto/incompleto (no deberia ocurrir con el
    codigo actual, ver docstring del modulo): un ENTRY sin `price` -> nunca
    se fabrica un precio de entrada, se marca el campo como insuficiente en
    vez de asumir 0.0 silenciosamente.
    """
    events = [
        _row("2026-09-01T14:00:00+00:00", "ENTRY", "p7", side="buy",
             quantity_delta=4, quantity_after=4, price=None, reason="smile_dislocation"),
        _row("2026-09-02T14:00:00+00:00", "CLOSE", "p7", side="sell",
             quantity_delta=-4, quantity_after=0, price=90.0, reason="stop_loss"),
    ]
    episodes = build_episode_lifecycles(events)
    ep = episodes[0]
    assert ep.average_entry is None
    assert ep.realized_pnl == 0.0
    assert "average_entry" in ep.data_insufficient_fields
    assert "realized_pnl" in ep.data_insufficient_fields


ALL_TESTS = [
    test_simple_entry_and_close_produces_one_closed_episode,
    test_entry_partial_exit_and_close_accumulates_realized_pnl_across_reductions,
    test_open_position_without_close_event_has_no_closed_at_or_close_reason,
    test_events_without_position_id_are_ignored_reject_and_cancel,
    test_multiple_position_ids_produce_independent_episodes_in_first_seen_order,
    test_missing_entry_price_marks_average_entry_and_realized_pnl_as_data_insufficient,
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
