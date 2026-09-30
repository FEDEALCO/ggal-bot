"""
test_dashboard_data_freshness.py
===================================
Tests para dashboard/data/freshness.py (panel de frescura de datos, Fase 1).

Correr con:
    python -m ggal_bot.validation.test_dashboard_data_freshness
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dashboard.data import freshness as fr


def test_is_within_byma_session_true_during_assumed_window_on_a_weekday():
    # 2026-09-30 es miercoles. 14:00 UTC = 11:00 ART -> borde inicial de la rueda asumida.
    now = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
    assert fr.is_within_byma_session(now) is True


def test_is_within_byma_session_false_outside_the_assumed_window():
    # 20:30 UTC = 17:30 ART -> despues del cierre asumido.
    now = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)
    assert fr.is_within_byma_session(now) is False


def test_is_within_byma_session_false_on_weekend():
    # 2026-10-03 es sabado.
    now = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
    assert fr.is_within_byma_session(now) is False


def test_compute_freshness_no_data_when_last_event_is_none():
    result = fr.compute_freshness("market_snapshots", None)
    assert result.has_data is False
    assert result.is_stale is False
    assert "SIN DATOS" in result.reason


def test_compute_freshness_not_stale_when_recent():
    now = datetime(2026, 9, 30, 14, 30, tzinfo=timezone.utc)  # dentro de la rueda asumida
    last_event = datetime(2026, 9, 30, 14, 25, tzinfo=timezone.utc)  # hace 5 min
    result = fr.compute_freshness("position_events", last_event, now_utc=now, stale_after_minutes=15.0)
    assert result.has_data is True
    assert result.is_stale is False
    assert abs(result.minutes_since - 5.0) < 1e-6


def test_compute_freshness_stale_when_old_during_session():
    now = datetime(2026, 9, 30, 14, 30, tzinfo=timezone.utc)  # dentro de la rueda asumida
    last_event = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)  # hace 30 min
    result = fr.compute_freshness("position_events", last_event, now_utc=now, stale_after_minutes=15.0)
    assert result.is_stale is True
    assert result.reason is not None and "30" in result.reason


def test_compute_freshness_not_stale_outside_session_even_if_old_by_default():
    now = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)  # fuera de la rueda asumida
    last_event = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)  # hace horas
    result = fr.compute_freshness("position_events", last_event, now_utc=now, stale_after_minutes=15.0)
    assert result.is_stale is False  # el bot no opera fuera de rueda, no es una alerta real


def test_compute_freshness_can_ignore_session_gate_when_asked():
    now = datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)
    last_event = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
    result = fr.compute_freshness(
        "position_events", last_event, now_utc=now, stale_after_minutes=15.0,
        only_flag_stale_during_session=False,
    )
    assert result.is_stale is True


ALL_TESTS = [
    test_is_within_byma_session_true_during_assumed_window_on_a_weekday,
    test_is_within_byma_session_false_outside_the_assumed_window,
    test_is_within_byma_session_false_on_weekend,
    test_compute_freshness_no_data_when_last_event_is_none,
    test_compute_freshness_not_stale_when_recent,
    test_compute_freshness_stale_when_old_during_session,
    test_compute_freshness_not_stale_outside_session_even_if_old_by_default,
    test_compute_freshness_can_ignore_session_gate_when_asked,
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
