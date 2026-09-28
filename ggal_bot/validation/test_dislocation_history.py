"""
test_dislocation_history.py
==============================
Tests para data/dislocation_history.py::DislocationHistoryTracker (MEJORA
2026-09-28: z-score adaptativo de dislocacion de smile para
WeeklyAsymmetricStrategy - ver docstring de ese modulo y
config.LongFirstConfig.enable_zscore_filter). API deliberadamente identica
a data/iv_mean_reversion.py::IVMeanReversionTracker (ver docstring de
dislocation_history.py) - estos tests mirroran los de
test_scalping_mode.py::IVMeanReversionTracker, adaptados (sin
has_reverted(), que este tracker no tiene: solo se usa el z-score crudo
como gate en scan_entry_signals(), no una logica de entrada/salida por
reversion).

Correr con:
    python -m ggal_bot.validation.test_dislocation_history
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import datetime, timedelta, timezone

from ggal_bot.data.dislocation_history import DislocationHistoryTracker


def test_dislocation_tracker_zscore_none_without_enough_samples():
    tracker = DislocationHistoryTracker(min_samples=5)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        tracker.update("SYM", -3.0, now=now + timedelta(seconds=i))
    assert tracker.zscore("SYM") is None


def test_dislocation_tracker_zscore_none_for_unknown_symbol():
    tracker = DislocationHistoryTracker(min_samples=1)
    assert tracker.zscore("NUNCA_VISTO") is None
    assert tracker.sample_count("NUNCA_VISTO") == 0


def test_dislocation_tracker_detects_extreme_deviation():
    tracker = DislocationHistoryTracker(min_samples=5, max_window_seconds=3600.0)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    baseline = [-1.0, -1.2, -0.8, -1.1, -0.9]
    for i, v in enumerate(baseline):
        tracker.update("SYM", v, now=now + timedelta(seconds=i))
    # Shock: la ultima muestra se aleja fuertemente del promedio reciente.
    tracker.update("SYM", -8.0, now=now + timedelta(seconds=10))
    z = tracker.zscore("SYM")
    assert z is not None and z < -2.0  # mas negativo = mas barata que lo usual (mismo signo que la dislocacion)


def test_dislocation_tracker_ignores_none_dislocation():
    tracker = DislocationHistoryTracker(min_samples=1)
    tracker.update("SYM", None, now=datetime.now(timezone.utc))
    assert tracker.sample_count("SYM") == 0


def test_dislocation_tracker_trims_window_by_max_age():
    tracker = DislocationHistoryTracker(min_samples=1, max_window_seconds=10.0)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    tracker.update("SYM", -1.0, now=now)
    tracker.update("SYM", -1.0, now=now + timedelta(seconds=20))  # ya deberia expulsar la muestra vieja
    assert tracker.sample_count("SYM") == 1


def test_dislocation_tracker_trims_by_max_samples():
    tracker = DislocationHistoryTracker(min_samples=1, max_window_seconds=1e9, max_samples=5)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    for i in range(10):
        tracker.update("SYM", -1.0 - i * 0.01, now=now + timedelta(seconds=i))
    assert tracker.sample_count("SYM") == 5


def test_dislocation_tracker_zscore_none_on_constant_series():
    """Serie constante (stdev=0): z-score indefinido, no una division por cero fabricada."""
    tracker = DislocationHistoryTracker(min_samples=3)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        tracker.update("SYM", -2.0, now=now + timedelta(seconds=i))
    assert tracker.zscore("SYM") is None


def test_dislocation_tracker_independent_windows_per_symbol():
    tracker = DislocationHistoryTracker(min_samples=1)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    tracker.update("AAA", -1.0, now=now)
    tracker.update("AAA", -1.0, now=now + timedelta(seconds=1))
    assert tracker.sample_count("AAA") == 2
    assert tracker.sample_count("BBB") == 0


ALL_TESTS = [
    test_dislocation_tracker_zscore_none_without_enough_samples,
    test_dislocation_tracker_zscore_none_for_unknown_symbol,
    test_dislocation_tracker_detects_extreme_deviation,
    test_dislocation_tracker_ignores_none_dislocation,
    test_dislocation_tracker_trims_window_by_max_age,
    test_dislocation_tracker_trims_by_max_samples,
    test_dislocation_tracker_zscore_none_on_constant_series,
    test_dislocation_tracker_independent_windows_per_symbol,
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
