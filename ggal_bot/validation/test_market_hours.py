"""
test_market_hours.py
=======================
Tests para ggal_bot/market_hours.py (MEJORA 2026-10-01: unica fuente de
verdad de "esta la rueda de BYMA abierta ahora", compartida por el bot y
el dashboard - antes duplicada solo en dashboard/data/freshness.py).

Correr con:
    python -m ggal_bot.validation.test_market_hours
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot import market_hours as mh


def test_is_within_byma_session_true_during_assumed_window_on_a_weekday():
    # 2026-10-01 es jueves. 14:00 UTC = 11:00 ART (apertura asumida, inclusive).
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    assert mh.is_within_byma_session(now) is True


def test_is_within_byma_session_false_at_exact_close():
    # 20:00 UTC = 17:00 ART (cierre asumido, EXCLUSIVO - ver `<` en la implementacion).
    now = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
    assert mh.is_within_byma_session(now) is False


def test_is_within_byma_session_false_overnight():
    # El caso real que motivo esta mejora: 10:28 ART (antes de la apertura).
    now = datetime(2026, 9, 29, 13, 28, tzinfo=timezone.utc)  # 10:28 ART
    assert mh.is_within_byma_session(now) is False


def test_is_within_byma_session_false_on_saturday():
    # 2026-10-03 es sabado.
    now = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)  # 12:00 ART
    assert mh.is_within_byma_session(now) is False


def test_is_within_byma_session_false_on_sunday():
    # 2026-10-04 es domingo.
    now = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
    assert mh.is_within_byma_session(now) is False


def test_is_within_byma_session_accepts_naive_datetime_as_utc():
    # Sin tzinfo -> se asume UTC, nunca se adivina otra zona horaria.
    now_naive = datetime(2026, 10, 1, 14, 0)
    assert mh.is_within_byma_session(now_naive) is True


ALL_TESTS = [
    test_is_within_byma_session_true_during_assumed_window_on_a_weekday,
    test_is_within_byma_session_false_at_exact_close,
    test_is_within_byma_session_false_overnight,
    test_is_within_byma_session_false_on_saturday,
    test_is_within_byma_session_false_on_sunday,
    test_is_within_byma_session_accepts_naive_datetime_as_utc,
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
