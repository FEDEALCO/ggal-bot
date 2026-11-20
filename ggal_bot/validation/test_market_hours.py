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

ACTUALIZADO 2026-11-20 (a pedido explicito del usuario, caso real
GFGV4000DI - ver RESEARCH.md): la fecha de corte fija se reemplazo por
`BYMA_OPTIONS_SESSION_REGIMES`, una tabla de vigencias con fecha de
inicio y cita de fuente (re-confirmado por el Comunicado N. 19031, que
reemplazo a N. 19024 con los mismos valores). Los valores verificados no
cambiaron - estos tests siguen pasando con los mismos horarios - pero se
agregan tests nuevos para la tabla en si y para la alerta de "fecha sin
ninguna vigencia conocida" (`_regime_for`).

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


# ---------------------------------------------------------------------------
# REDISEÑO 2026-11-20: tabla de vigencias (BYMA_OPTIONS_SESSION_REGIMES) en
# vez de una fecha de corte fija - ver docstring de market_hours.py.
# ---------------------------------------------------------------------------

def test_byma_options_session_regimes_table_has_two_ascending_entries_with_sources():
    regimes = mh.BYMA_OPTIONS_SESSION_REGIMES
    assert len(regimes) == 2
    assert regimes[0].effective_from < regimes[1].effective_from  # orden ascendente, asumido por _regime_for
    assert regimes[0].effective_from == date(2025, 7, 28)
    assert regimes[0].open_hour_art == 10.5 and regimes[0].close_hour_art == 17.0
    assert regimes[1].effective_from == date(2026, 11, 2)
    assert regimes[1].open_hour_art == 10.5 and regimes[1].close_hour_art == 18.0
    # Toda vigencia debe citar de donde sale - nunca un valor sin fuente verificable.
    for regime in regimes:
        assert regime.source and len(regime.source) > 20
    assert "19031" in regimes[1].source


def test_regime_for_picks_the_latest_regime_that_already_started():
    regimes = mh.BYMA_OPTIONS_SESSION_REGIMES
    assert mh._regime_for(date(2025, 7, 28)) is regimes[0]
    assert mh._regime_for(date(2026, 10, 1)) is regimes[0]
    assert mh._regime_for(date(2026, 11, 1)) is regimes[0]
    assert mh._regime_for(date(2026, 11, 2)) is regimes[1]
    # La ultima vigencia no tiene fecha de fin: cubre cualquier fecha
    # futura hasta que se agregue una vigencia mas nueva a la tabla.
    assert mh._regime_for(date(2027, 1, 1)) is regimes[1]
    assert mh._regime_for(date(2099, 12, 31)) is regimes[1]


def test_regime_for_alerts_once_per_date_when_no_regime_covers_it():
    """
    A pedido explicito del usuario ("alerta si la fecha actual no esta
    cubierta por ninguna vigencia"): el UNICO gap posible es una fecha
    ANTERIOR a la vigencia mas antigua conocida (2025-07-28), ya que la
    ULTIMA vigencia no tiene fin y cubre cualquier fecha futura. Alerta
    una sola vez por fecha distinta (no en cada llamada - mismo criterio
    anti-spam que el throttle de quote_gap_backstop del 18/11, ver
    RESEARCH.md) y cae a la vigencia mas antigua como fallback
    conservador en vez de fallar o adivinar un valor no verificado.
    """
    gap_date = date(2020, 1, 1)  # bien anterior a 2025-07-28
    mh._uncovered_dates_logged.discard(gap_date)  # estado limpio por si otro test ya la alerto
    warnings = []
    original_warning = mh.logger.warning
    mh.logger.warning = lambda msg, *args: warnings.append(msg % args)
    try:
        regime = mh._regime_for(gap_date)
        assert regime is mh.BYMA_OPTIONS_SESSION_REGIMES[0]  # fallback: vigencia mas antigua conocida
        assert len(warnings) == 1, f"esperaba exactamente 1 alerta, hubo {len(warnings)}"
        assert str(gap_date) in warnings[0]

        # Misma fecha otra vez: NO vuelve a loguear (dedupe).
        mh._regime_for(gap_date)
        assert len(warnings) == 1

        # Fecha cubierta (sin gap): nunca alerta.
        mh._regime_for(date(2026, 10, 1))
        assert len(warnings) == 1
    finally:
        mh.logger.warning = original_warning
        mh._uncovered_dates_logged.discard(gap_date)


def test_is_within_byma_session_uses_oldest_regime_as_fallback_for_a_date_before_any_known_vigencia():
    # 2020-01-01 es miercoles. Sin ninguna vigencia conocida para esa
    # fecha, cae a la mas antigua (10:30-17:00 ART).
    mh._uncovered_dates_logged.discard(date(2020, 1, 1))
    original_warning = mh.logger.warning
    mh.logger.warning = lambda *args, **kwargs: None  # silencia la alerta esperada, no es lo que este test verifica
    try:
        within_old_hours = datetime(2020, 1, 1, 14, 0, tzinfo=timezone.utc)  # 11:00 ART
        assert mh.is_within_byma_session(within_old_hours) is True
        after_old_close = datetime(2020, 1, 1, 20, 0, tzinfo=timezone.utc)  # 17:00 ART (cierre viejo, no el de 18:00)
        assert mh.is_within_byma_session(after_old_close) is False
    finally:
        mh.logger.warning = original_warning
        mh._uncovered_dates_logged.discard(date(2020, 1, 1))


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
    test_byma_options_session_regimes_table_has_two_ascending_entries_with_sources,
    test_regime_for_picks_the_latest_regime_that_already_started,
    test_regime_for_alerts_once_per_date_when_no_regime_covers_it,
    test_is_within_byma_session_uses_oldest_regime_as_fallback_for_a_date_before_any_known_vigencia,
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
