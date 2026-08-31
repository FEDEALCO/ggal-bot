"""
test_microstructure.py
=========================
Tests de sanity para models/microstructure.py (Order Book Imbalance): la
funcion de calculo en si, en aislamiento del resto de la estrategia (ver
test_long_first_mode.py para los tests de integracion del filtro OBI dentro
de scan_entry_signals()).

Correr con:
    python -m ggal_bot.validation.test_microstructure
"""

from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.data.option_chain import OrderBookSnapshot
from ggal_bot.models.microstructure import order_book_imbalance, passes_obi_filter


def _book(bid_size: float, ask_size: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(symbol="TEST", bid=95.0, ask=105.0, bid_size=bid_size, ask_size=ask_size)


def test_order_book_imbalance_balanced_book_is_zero():
    assert order_book_imbalance(_book(100.0, 100.0)) == 0.0


def test_order_book_imbalance_buy_side_heavy_is_positive():
    book = _book(300.0, 100.0)
    obi = order_book_imbalance(book)
    assert abs(obi - 0.5) < 1e-9  # (300-100)/(300+100) = 0.5


def test_order_book_imbalance_sell_side_heavy_is_negative():
    book = _book(10.0, 490.0)
    obi = order_book_imbalance(book)
    assert abs(obi - (-0.96)) < 1e-9  # (10-490)/(10+490) = -0.96


def test_order_book_imbalance_extreme_one_sided_bounds():
    assert order_book_imbalance(_book(1000.0, 0.0)) == 1.0
    assert order_book_imbalance(_book(0.0, 1000.0)) == -1.0


def test_order_book_imbalance_no_size_reported_defaults_neutral():
    """Sin tamaños informados (fuente que no reporta profundidad), OBI=0.0: no penaliza a ciegas."""
    assert order_book_imbalance(_book(0.0, 0.0)) == 0.0


def test_passes_obi_filter_blocks_below_floor():
    book = _book(10.0, 490.0)  # OBI = -0.96
    assert not passes_obi_filter(book, min_obi=-0.30)


def test_passes_obi_filter_allows_at_or_above_floor():
    book = _book(80.0, 120.0)  # OBI = -0.20
    assert passes_obi_filter(book, min_obi=-0.30)


def test_passes_obi_filter_boundary_is_inclusive():
    book = _book(35.0, 65.0)  # OBI = -0.30 exacto
    assert passes_obi_filter(book, min_obi=-0.30)


ALL_TESTS = [
    test_order_book_imbalance_balanced_book_is_zero,
    test_order_book_imbalance_buy_side_heavy_is_positive,
    test_order_book_imbalance_sell_side_heavy_is_negative,
    test_order_book_imbalance_extreme_one_sided_bounds,
    test_order_book_imbalance_no_size_reported_defaults_neutral,
    test_passes_obi_filter_blocks_below_floor,
    test_passes_obi_filter_allows_at_or_above_floor,
    test_passes_obi_filter_boundary_is_inclusive,
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
