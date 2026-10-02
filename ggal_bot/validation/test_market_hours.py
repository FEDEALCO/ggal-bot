"""
test_market_hours.py
=======================
Tests para ggal_bot/market_hours.py (MEJORA 2026-10-01: unica fuente de
verdad de "esta la rueda de BYMA abierta ahora", compartida por el bot y
el dashboard - antes duplicada solo en dashboard/data/freshness.py).

ACTUALIZADO 2026-10-02 (Tarea #27/#28 item 6, verificacion contra fuente
oficial - ver docstring de ggal_bot/market_hours.py para las fuentes
primarias citadas verbatim): apertura corregida de 11:00 a 10:30 ART, y
cierre ahora fecha-consciente (17:00 ART hasta 2026-11-01 inclusive,
18:00 ART desde 2026-11-02 en adelante por el Comunicado BYMA N. 19024).

Correr con:
    python -m ggal_bot.validation.test_market_hours
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot import market_hours as mh


def test_is_within_byma_session_true_during_verified_window_on_a_weekday():
    # 2026-10-01 es jueves. 14:00 UTC = 11:00 ART (dentro de 10:30-17:00 ART verificado).
    now = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    assert mh.is_within_byma_session(now) is True


def test_is_within_byma_session_false_at_exact_close_before_the_2026_11_02_change():
    # 20:00 UTC = 17:00 ART (cierre vigente HOY, EXCLUSIVO - ver `<` en la implementacion).
    now = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
    assert mh.is_within_byma_session(now) is False


def test_is_within_byma_session_true_at_1030_art_exact_open_inclusive():
    # Caso limite verificado: 10:30 ART exacto = apertura, INCLUSIVE (ver `<=`).
    # 2026-10-01 es jueves. 13:30 UTC = 10:30 ART.
    now = datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc)
    assert mh.is_within_byma_session(now) is True


def test_is_within_byma_session_false_overnight():
    # El caso real que motivo la mejora 2026-10-01: 10:28 ART, ahora TAMBIEN
    # antes de la apertura verificada (10:30 ART), no solo de la vieja (11:00).
    now = datetime(2026, 9, 29, 13, 28, tzinfo=timezone.utc)  # 10:28 ART
    assert mh.is_within_byma_session(now) is False


def test_is_within_byma_session_false_between_old_and_new_open_hour():
    # Caso que distingue el fix de este item: 10:45 ART (jueves) estaba
    # FUERA de horario con el supuesto viejo (11:00 ART) pero esta DENTRO
    # con el horario verificado (10:30 ART) - confirma que el fix realmente
    # se aplico, no solo que el valor cambio sin efecto.
    now = datetime(2026, 10, 1, 13, 45, tzinfo=timezone.utc)  # 10:45 ART, jueves
    assert mh.is_within_byma_session(now) is True


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


def test_session_end_hour_art_is_17_before_the_confirmed_change_date():
    assert mh._session_end_hour_art(date(2026, 11, 1)) == 17.0


def test_session_end_hour_art_is_18_on_and_after_the_confirmed_change_date():
    assert mh._session_end_hour_art(date(2026, 11, 2)) == 18.0
    assert mh._session_end_hour_art(date(2026, 12, 1)) == 18.0


def test_is_within_byma_session_true_at_1700_art_on_2026_11_02_once_close_moves_to_1800():
    # 2026-11-02 es lunes. 20:00 UTC = 17:00 ART: con el cierre viejo esto ya
    # estaria cerrado, pero desde esta fecha el cierre verificado es 18:00
    # ART - debe seguir DENTRO de la rueda.
    now = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)
    assert mh.is_within_byma_session(now) is True


def test_is_within_byma_session_false_at_exact_new_close_on_2026_11_02():
    # 21:00 UTC = 18:00 ART del mismo lunes: cierre nuevo, EXCLUSIVO.
    now = datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)
    assert mh.is_within_byma_session(now) is False


ALL_TESTS = [
    test_is_within_byma_session_true_during_verified_window_on_a_weekday,
    test_is_within_byma_session_false_at_exact_close_before_the_2026_11_02_change,
    test_is_within_byma_session_true_at_1030_art_exact_open_inclusive,
    test_is_within_byma_session_false_overnight,
    test_is_within_byma_session_false_between_old_and_new_open_hour,
    test_is_within_byma_session_false_on_saturday,
    test_is_within_byma_session_false_on_sunday,
    test_is_within_byma_session_accepts_naive_datetime_as_utc,
    test_session_end_hour_art_is_17_before_the_confirmed_change_date,
    test_session_end_hour_art_is_18_on_and_after_the_confirmed_change_date,
    test_is_within_byma_session_true_at_1700_art_on_2026_11_02_once_close_moves_to_1800,
    test_is_within_byma_session_false_at_exact_new_close_on_2026_11_02,
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
