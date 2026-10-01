"""
test_weekend_guard_check.py
==============================
Tests para ggal_bot/ops/weekend_guard_check.py (chequeo operativo a pedido
explicito del usuario, sesion 2026-10-01: "Mañana viernes es la primera
prueba del fix del weekend guard: dejá listo un chequeo que confirme el
lunes que no hubo entradas semanales nuevas el viernes").

Correr con:
    python -m ggal_bot.validation.test_weekend_guard_check
"""
from __future__ import annotations

import os
import sys
from datetime import date

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

pytest.importorskip("pandas")
import pandas as pd

from ggal_bot.ops import weekend_guard_check as wgc

# Viernes real usado en la evidencia de la sesion (2026-10-01 es jueves ->
# 2026-10-02 es el viernes de la "primera prueba" que menciona el usuario).
_FRIDAY = date(2026, 10, 2)


def _row(event_type, strategy_tag, ts, symbol="GFGC6600OC", contract_key=None, position_id="pid1", order_client_id="cid1"):
    return {
        "timestamp_utc": pd.Timestamp(ts, tz="UTC"),
        "event_type": event_type,
        "position_id": position_id,
        "contract_key": contract_key,
        "symbol": symbol,
        "strategy_tag": strategy_tag,
        "side": "buy",
        "quantity_delta": 1.0,
        "quantity_after": 1.0,
        "price": 100.0,
        "order_client_id": order_client_id,
        "reason": "",
        "data_unavailable_fields": "" if contract_key else "contract_key",
    }


def test_most_recent_friday_when_today_is_already_friday():
    assert wgc.most_recent_friday(date(2026, 10, 2)) == date(2026, 10, 2)


def test_most_recent_friday_rolls_back_to_last_friday():
    # 2026-10-05 es lunes -> el ultimo viernes fue 2026-10-02.
    assert wgc.most_recent_friday(date(2026, 10, 5)) == date(2026, 10, 2)


def test_parse_expiry_from_contract_key_valid():
    assert wgc._parse_expiry_from_contract_key("GGAL|GFGC6600OC|2026-10-16") == date(2026, 10, 16)


def test_parse_expiry_from_contract_key_invalid_or_empty_returns_none():
    assert wgc._parse_expiry_from_contract_key("") is None
    assert wgc._parse_expiry_from_contract_key(None) is None
    assert wgc._parse_expiry_from_contract_key("formato-raro") is None


def test_find_violations_detects_friday_entry_spanning_the_weekend():
    """
    El caso que el fix debe evitar: ENTRY un viernes (ART) sobre un
    vencimiento POSTERIOR a ese viernes.
    """
    df = pd.DataFrame([
        _row("ENTRY", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", contract_key="GGAL|GFGC6600OC|2026-10-16"),
    ])
    violations = wgc.find_friday_entries_that_should_have_been_blocked(df, _FRIDAY)
    assert len(violations) == 1
    assert violations[0].symbol == "GFGC6600OC"
    assert violations[0].expiry == date(2026, 10, 16)


def test_find_violations_empty_when_fix_is_working_no_friday_entries():
    df = pd.DataFrame([
        _row("ENTRY", "weekly_asymmetric", "2026-10-01T14:05:00+00:00", contract_key="GGAL|GFGC6600OC|2026-10-16"),  # jueves
        _row("ENTRY", "weekly_asymmetric", "2026-10-05T14:05:00+00:00", contract_key="GGAL|GFGC6600OC|2026-10-16"),  # lunes
    ])
    assert wgc.find_friday_entries_that_should_have_been_blocked(df, _FRIDAY) == []


def test_find_violations_ignores_friday_entry_expiring_that_same_friday():
    """
    Si el vencimiento ES ese mismo viernes (no se extiende al fin de
    semana), no es el patron que el guard necesita bloquear.
    """
    df = pd.DataFrame([
        _row("ENTRY", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", contract_key="GGAL|GFGC6600OC|2026-10-02"),
    ])
    assert wgc.find_friday_entries_that_should_have_been_blocked(df, _FRIDAY) == []


def test_find_violations_ignores_other_strategies_and_other_event_types():
    df = pd.DataFrame([
        _row("ENTRY", "scalping", "2026-10-02T14:05:00+00:00", contract_key="GGAL|GFGC6600OC|2026-10-16"),
        _row("CLOSE", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", contract_key="GGAL|GFGC6600OC|2026-10-16"),
    ])
    assert wgc.find_friday_entries_that_should_have_been_blocked(df, _FRIDAY) == []


def test_find_violations_excludes_entries_without_parseable_contract_key():
    df = pd.DataFrame([
        _row("ENTRY", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", contract_key=None),
    ])
    assert wgc.find_friday_entries_that_should_have_been_blocked(df, _FRIDAY) == []


def test_entries_without_expiry_on_friday_reports_them_separately():
    df = pd.DataFrame([
        _row("ENTRY", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", contract_key=None),
    ])
    unknown = wgc.entries_without_expiry_on_friday(df, _FRIDAY)
    assert len(unknown) == 1


def test_find_violations_empty_dataframe_returns_empty_list():
    assert wgc.find_friday_entries_that_should_have_been_blocked(pd.DataFrame(), _FRIDAY) == []


def test_format_report_ok_case():
    report = wgc.format_report([], _FRIDAY, unknown_count=0)
    assert "OK" in report
    assert "2026-10-02" in report


def test_format_report_failure_case_lists_each_violation():
    v = wgc.WeekendGuardViolation(
        timestamp_utc=pd.Timestamp("2026-10-02T14:05:00+00:00"), symbol="GFGC6600OC",
        position_id="pid1", expiry=date(2026, 10, 16), order_client_id="cid1",
    )
    report = wgc.format_report([v], _FRIDAY, unknown_count=0)
    assert "FALLO" in report
    assert "GFGC6600OC" in report
    assert "GGAL_BOT_WEEKEND_THETA_GUARD_BLOCK_NEW_ENTRIES" in report


ALL_TESTS = [
    test_most_recent_friday_when_today_is_already_friday,
    test_most_recent_friday_rolls_back_to_last_friday,
    test_parse_expiry_from_contract_key_valid,
    test_parse_expiry_from_contract_key_invalid_or_empty_returns_none,
    test_find_violations_detects_friday_entry_spanning_the_weekend,
    test_find_violations_empty_when_fix_is_working_no_friday_entries,
    test_find_violations_ignores_friday_entry_expiring_that_same_friday,
    test_find_violations_ignores_other_strategies_and_other_event_types,
    test_find_violations_excludes_entries_without_parseable_contract_key,
    test_entries_without_expiry_on_friday_reports_them_separately,
    test_find_violations_empty_dataframe_returns_empty_list,
    test_format_report_ok_case,
    test_format_report_failure_case_lists_each_violation,
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
