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

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

pytest.importorskip("pandas")
import pandas as pd

from ggal_bot.ops import weekend_guard_check as wgc
from ggal_bot.risk.kill_switch import KillSwitchState

# Viernes real usado en la evidencia de la sesion (2026-10-01 es jueves ->
# 2026-10-02 es el viernes de la "primera prueba" que menciona el usuario).
_FRIDAY = date(2026, 10, 2)


def _row(
    event_type, strategy_tag, ts, symbol="GFGC6600OC", contract_key=None,
    position_id="pid1", order_client_id="cid1", side="buy", reason="",
):
    return {
        "timestamp_utc": pd.Timestamp(ts, tz="UTC"),
        "event_type": event_type,
        "position_id": position_id,
        "contract_key": contract_key,
        "symbol": symbol,
        "strategy_tag": strategy_tag,
        "side": side,
        "quantity_delta": 1.0,
        "quantity_after": 1.0,
        "price": 100.0,
        "order_client_id": order_client_id,
        "reason": reason,
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


# ---------------------------------------------------------------------------
# ACTUALIZACION 2026-10-02 (a pedido explicito del usuario: distinguir "sin
# entradas porque el guard bloqueo" de "sin entradas por kill switch u otra
# causa" - ver docstring extenso del modulo).
# ---------------------------------------------------------------------------

def test_find_entry_rejects_on_day_matches_buy_rejects_of_the_strategy_that_day():
    df = pd.DataFrame([
        _row("REJECT", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", side="buy", reason="kill_switch_tripped: x"),
        # dia distinto: no cuenta
        _row("REJECT", "weekly_asymmetric", "2026-10-01T14:05:00+00:00", side="buy", reason="kill_switch_tripped: x"),
        # side=sell (un REJECT de SALIDA, ej. position_invariant_violation): no cuenta
        _row("REJECT", "weekly_asymmetric", "2026-10-02T15:00:00+00:00", side="sell", reason="position_invariant_violation: x"),
        # otra estrategia: no cuenta
        _row("REJECT", "scalping", "2026-10-02T14:05:00+00:00", side="buy", reason="greeks_limit_exceeded: x"),
        # ENTRY confirmado (no REJECT): no cuenta aca
        _row("ENTRY", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", contract_key="GGAL|GFGC6600OC|2026-10-16"),
    ])
    rejects = wgc.find_entry_rejects_on_day(df, _FRIDAY)
    assert len(rejects) == 1
    assert rejects.iloc[0]["reason"] == "kill_switch_tripped: x"


def test_find_entry_rejects_on_day_empty_dataframe():
    assert wgc.find_entry_rejects_on_day(pd.DataFrame(), _FRIDAY).empty


def test_summarize_entry_rejects_buckets_known_reasons_and_groups_unknown_as_other():
    df = pd.DataFrame([
        _row("REJECT", "weekly_asymmetric", "2026-10-02T14:00:00+00:00", side="buy", reason="kill_switch_tripped: a"),
        _row("REJECT", "weekly_asymmetric", "2026-10-02T14:01:00+00:00", side="buy", reason="greeks_limit_exceeded: b"),
        _row("REJECT", "weekly_asymmetric", "2026-10-02T14:02:00+00:00", side="buy", reason="greeks_budget_preemptive: c"),
        _row("REJECT", "weekly_asymmetric", "2026-10-02T14:03:00+00:00", side="buy", reason="unknown_greeks: d"),
        _row("REJECT", "weekly_asymmetric", "2026-10-02T14:04:00+00:00", side="buy", reason="sizing_not_tradeable: e"),
        _row("REJECT", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", side="buy", reason="algo_nunca_visto: f"),
    ])
    counts = wgc.summarize_entry_rejects(df)
    assert counts == {"kill_switch": 1, "greeks_limit": 3, "sizing": 1, "other": 1}


def test_summarize_entry_rejects_empty_dataframe_returns_empty_dict():
    assert wgc.summarize_entry_rejects(pd.DataFrame()) == {}


def test_kill_switch_status_note_none_when_not_tripped():
    assert wgc.kill_switch_status_note(_FRIDAY, KillSwitchState(tripped=False)) is None
    assert wgc.kill_switch_status_note(_FRIDAY, None) is None


def test_kill_switch_status_note_warns_when_tripped_on_or_before_friday():
    state = KillSwitchState(
        tripped=True, reason="delta excedido", tripped_by="max_portfolio_delta_ars",
        tripped_at="2026-10-02T16:26:21+00:00",
    )
    note = wgc.kill_switch_status_note(_FRIDAY, state)
    assert note is not None
    assert "max_portfolio_delta_ars" in note
    assert "no prueba" in note


def test_kill_switch_status_note_none_when_last_trip_is_after_friday():
    """Si el ultimo trip conocido es POSTERIOR al viernes evaluado, no es
    evidencia relevante para ese dia - no se fabrica una advertencia sin
    base."""
    state = KillSwitchState(
        tripped=True, reason="x", tripped_by="y", tripped_at="2026-10-05T12:00:00+00:00",
    )
    assert wgc.kill_switch_status_note(_FRIDAY, state) is None


def test_format_report_ok_case_with_rejects_warns_that_ok_is_not_conclusive():
    report = wgc.format_report(
        [], _FRIDAY, unknown_count=0, reject_counts={"kill_switch": 2},
    )
    assert "OK" in report
    assert "ATENCION" in report
    assert "kill_switch: 2" in report
    assert "NO prueba" in report


def test_format_report_ok_case_without_rejects_states_ok_is_real_evidence():
    report = wgc.format_report([], _FRIDAY, unknown_count=0, reject_counts={})
    assert "OK" in report
    assert "ATENCION" not in report
    assert "evidencia real" in report


def test_format_report_includes_kill_switch_note_when_given():
    note = "Kill switch ACTUALMENTE disparado (x: y)."
    report = wgc.format_report([], _FRIDAY, unknown_count=0, kill_switch_note=note)
    assert note in report


def test_format_report_flags_when_kill_switch_state_unavailable():
    report = wgc.format_report([], _FRIDAY, unknown_count=0, kill_switch_state_available=False)
    assert "no se encontro el archivo de estado del kill switch" in report.lower()


def test_find_kill_switch_rejects_on_day_any_strategy_matches_across_strategies():
    """
    A diferencia de find_entry_rejects_on_day (filtra por strategy_tag Y
    side="buy"), esto debe matchear kill_switch_tripped de CUALQUIER
    estrategia y CUALQUIER side ese dia - el caso real del 2026-10-02 tuvo
    rechazos de weekly_asymmetric Y scalping.
    """
    df = pd.DataFrame([
        _row("REJECT", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", side="buy", reason="kill_switch_tripped: x"),
        _row("REJECT", "scalping", "2026-10-02T15:30:00+00:00", side="buy", reason="kill_switch_tripped: y"),
        # otro dia: no cuenta
        _row("REJECT", "weekly_asymmetric", "2026-10-01T14:05:00+00:00", side="buy", reason="kill_switch_tripped: z"),
        # mismo dia, pero otro motivo: no cuenta
        _row("REJECT", "scalping", "2026-10-02T16:00:00+00:00", side="buy", reason="greeks_limit_exceeded: w"),
    ])
    rejects = wgc.find_kill_switch_rejects_on_day_any_strategy(df, _FRIDAY)
    assert len(rejects) == 2
    assert set(rejects["strategy_tag"]) == {"weekly_asymmetric", "scalping"}


def test_find_kill_switch_rejects_on_day_any_strategy_empty_dataframe():
    assert wgc.find_kill_switch_rejects_on_day_any_strategy(pd.DataFrame(), _FRIDAY).empty


def test_kill_switch_invalidates_day_note_none_when_no_rejects():
    assert wgc.kill_switch_invalidates_day_note(pd.DataFrame()) is None


def test_kill_switch_invalidates_day_note_explicit_when_rejects_exist():
    df = pd.DataFrame([
        _row("REJECT", "weekly_asymmetric", "2026-10-02T10:51:00+00:00", side="buy", reason="kill_switch_tripped: x"),
        _row("REJECT", "scalping", "2026-10-02T18:20:00+00:00", side="buy", reason="kill_switch_tripped: y"),
    ])
    rejects = wgc.find_kill_switch_rejects_on_day_any_strategy(df, _FRIDAY)
    note = wgc.kill_switch_invalidates_day_note(rejects)
    assert note is not None
    assert "NO ES UNA PRUEBA VALIDA" in note
    assert "2" in note  # cantidad de rejects


def test_format_report_puts_kill_switch_invalidation_before_resultado_ok():
    """
    BUG REAL CORREGIDO (2026-10-05, a pedido explicito del usuario): el
    aviso de invalidacion debe aparecer ANTES del RESULTADO OK/FALLO en el
    texto del reporte, no como una nota al pie que una lectura rapida
    puede pasar por alto.
    """
    note = "⚠️ ESTE DIA NO ES UNA PRUEBA VALIDA DEL WEEKEND GUARD: ..."
    report = wgc.format_report([], _FRIDAY, unknown_count=0, kill_switch_invalidates_note=note)
    assert report.index(note) < report.index("RESULTADO: OK")


def test_run_check_reports_kill_switch_invalidation_across_strategies(capsys):
    """
    Integracion end-to-end: un REJECT de kill_switch_tripped de SCALPING
    (no weekly_asymmetric) ese viernes debe disparar el aviso de
    invalidacion del dia - el caso real del 2026-10-02 tuvo rechazos en
    ambas estrategias.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        events_path = Path(tmp_dir) / "position_events.csv"
        pd.DataFrame([
            _row("REJECT", "scalping", "2026-10-02T13:30:00+00:00", side="buy", reason="kill_switch_tripped: x"),
        ]).to_csv(events_path, index=False)
        missing_ks_path = Path(tmp_dir) / "no_existe.json"

        exit_code = wgc.run_check(events_path, _FRIDAY, kill_switch_state_path=missing_ks_path)
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "NO ES UNA PRUEBA VALIDA" in out
        assert out.index("NO ES UNA PRUEBA VALIDA") < out.index("RESULTADO: OK")


def test_run_check_reports_reject_counts_and_kill_switch_from_real_files(capsys):
    """Integracion end-to-end de run_check(): escribe un position_events.csv
    y un kill_switch.json reales a disco y confirma que el reporte impreso
    incluye ambas señales nuevas."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        events_path = Path(tmp_dir) / "position_events.csv"
        ks_path = Path(tmp_dir) / "kill_switch.json"

        df = pd.DataFrame([
            _row("REJECT", "weekly_asymmetric", "2026-10-02T14:05:00+00:00", side="buy", reason="kill_switch_tripped: x"),
        ])
        df.to_csv(events_path, index=False)
        ks_path.write_text(json.dumps({
            "tripped": True, "reason": "delta excedido",
            "tripped_at": "2026-10-02T16:26:21+00:00", "tripped_by": "max_portfolio_delta_ars",
        }), encoding="utf-8")

        exit_code = wgc.run_check(events_path, _FRIDAY, kill_switch_state_path=ks_path)
        out = capsys.readouterr().out
        assert exit_code == 0  # sin violaciones CONFIRMADAS del guard en si
        assert "ATENCION" in out and "kill_switch: 1" in out
        assert "max_portfolio_delta_ars" in out


def test_run_check_flags_kill_switch_state_unavailable_when_file_missing(capsys):
    with tempfile.TemporaryDirectory() as tmp_dir:
        events_path = Path(tmp_dir) / "position_events.csv"
        pd.DataFrame([
            _row("ENTRY", "weekly_asymmetric", "2026-10-01T14:05:00+00:00", contract_key="GGAL|GFGC6600OC|2026-10-16"),
        ]).to_csv(events_path, index=False)
        missing_ks_path = Path(tmp_dir) / "no_existe.json"

        exit_code = wgc.run_check(events_path, _FRIDAY, kill_switch_state_path=missing_ks_path)
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "no se encontro el archivo de estado del kill switch" in out.lower()


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
    test_find_entry_rejects_on_day_matches_buy_rejects_of_the_strategy_that_day,
    test_find_entry_rejects_on_day_empty_dataframe,
    test_summarize_entry_rejects_buckets_known_reasons_and_groups_unknown_as_other,
    test_summarize_entry_rejects_empty_dataframe_returns_empty_dict,
    test_kill_switch_status_note_none_when_not_tripped,
    test_kill_switch_status_note_warns_when_tripped_on_or_before_friday,
    test_kill_switch_status_note_none_when_last_trip_is_after_friday,
    test_format_report_ok_case_with_rejects_warns_that_ok_is_not_conclusive,
    test_format_report_ok_case_without_rejects_states_ok_is_real_evidence,
    test_format_report_includes_kill_switch_note_when_given,
    test_format_report_flags_when_kill_switch_state_unavailable,
    test_find_kill_switch_rejects_on_day_any_strategy_matches_across_strategies,
    test_find_kill_switch_rejects_on_day_any_strategy_empty_dataframe,
    test_kill_switch_invalidates_day_note_none_when_no_rejects,
    test_kill_switch_invalidates_day_note_explicit_when_rejects_exist,
    test_format_report_puts_kill_switch_invalidation_before_resultado_ok,
    # test_run_check_reports_reject_counts_and_kill_switch_from_real_files,
    # test_run_check_flags_kill_switch_state_unavailable_when_file_missing y
    # test_run_check_reports_kill_switch_invalidation_across_strategies usan
    # el fixture `capsys` de pytest - no se agregan aca (mismo criterio que
    # el resto del repo: un test que necesita un fixture de pytest no entra
    # a ALL_TESTS, correrlo standalone rompería con un TypeError de
    # argumento faltante).
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
