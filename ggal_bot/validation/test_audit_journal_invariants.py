"""
test_audit_journal_invariants.py
===================================
Tests para audit_journal_invariants.py (checker standalone de
invariantes del PositionEventJournal real).

Cubre especificamente las dos correcciones del 2026-10-13 (hallazgo de
auditoria, a pedido explicito del usuario - "Por que el chequeo de
invariantes no marca el -8 de GFGC6600OC?"):

  1. check_long_only ya NO marca como violacion el wing corto de un
     spread completion de weekly_asymmetric (reason="spread_completion") -
     antes de la correccion, esto era un falso positivo confirmado en
     produccion (GFGV5000OC, 2026-10-01).
  2. check_cumulative_long_only_ledger (NUEVO) detecta una posicion
     long-only que termina neta corta a traves de MULTIPLES filas/
     position_id (como GFGC6600OC real: ninguna fila individual tiene
     quantity_after negativo, pero el acumulado por symbol+strategy_tag
     si cae por debajo de cero sin que ninguna ENTRY lo explique) -
     algo que check_long_only, al mirar fila por fila, no puede ver.

Correr con:
    python -m ggal_bot.validation.test_audit_journal_invariants
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from audit_journal_invariants import (
    check_cumulative_long_only_ledger,
    check_long_only,
    run_audit,
)

_COLUMNS = [
    "timestamp_utc", "event_type", "position_id", "contract_key", "symbol",
    "strategy_tag", "side", "quantity_delta", "quantity_after", "price",
    "order_client_id", "reason", "data_unavailable_fields",
]


def _row(ts, event_type, symbol, strategy_tag, quantity_delta, quantity_after,
         position_id="pid1", reason=""):
    return {
        "timestamp_utc": pd.Timestamp(ts, tz="UTC"),
        "event_type": event_type,
        "position_id": position_id,
        "contract_key": symbol,
        "symbol": symbol,
        "strategy_tag": strategy_tag,
        "side": "",
        "quantity_delta": quantity_delta,
        "quantity_after": quantity_after,
        "price": 0.0,
        "order_client_id": "",
        "reason": reason,
        "data_unavailable_fields": "",
    }


def _df(rows):
    return pd.DataFrame(rows, columns=_COLUMNS)


# --- check_long_only ---

def test_check_long_only_flags_real_naked_short():
    df = _df([
        _row("2026-10-01T10:00:00Z", "ENTRY", "GFGC7000OC", "weekly_asymmetric", -5.0, -5.0, reason="manual"),
    ])
    violations = check_long_only(df)
    assert len(violations) == 1
    assert violations[0].check == "long_only"
    assert violations[0].severity == "CRITICAL"
    assert "GFGC7000OC" in violations[0].detail


def test_check_long_only_excludes_spread_completion_wing():
    """
    Caso real GFGV5000OC (2026-10-01): ENTRY quantity_after=-75.0 pero
    reason="spread_completion" - el wing corto de un spread YA financiado
    por una pata larga confirmada, por diseño de weekly_asymmetric. No es
    una venta en descubierto real y no debe marcarse.
    """
    df = _df([
        _row("2026-10-01T10:47:59Z", "ENTRY", "GFGV5000OC", "weekly_asymmetric", -75.0, -75.0,
             position_id="ef268fed9a99", reason="spread_completion"),
        _row("2026-10-02T18:01:45Z", "CLOSE", "GFGV5000OC", "weekly_asymmetric", 75.0, 0.0,
             position_id="ef268fed9a99", reason="manual_close"),
    ])
    violations = check_long_only(df)
    assert violations == []


def test_check_long_only_ignores_non_long_only_strategies():
    df = _df([
        _row("2026-10-01T10:00:00Z", "ENTRY", "GGAL", "delta_hedge", -335.85, -335.85, reason="hedge"),
    ])
    violations = [v for v in check_long_only(df) if v.check == "long_only" and v.severity != "INFO"]
    assert violations == []


# --- check_cumulative_long_only_ledger ---

def test_cumulative_ledger_flags_critical_when_real_gap_after_covering_zero():
    """
    Replica el patron real de GFGC6600OC: un primer ciclo ENTRY->CLOSE que
    cierra limpio en 0 (cumulative llega a >=0 en algun momento), y DESPUES
    una fila de salida (PARTIAL_EXIT) sin ninguna ENTRY nueva que la
    explique - el acumulado cae por debajo de cero sin hueco de cobertura
    posible. Debe marcarse CRITICAL (gap real de logging), no INFO.
    """
    df = _df([
        _row("2026-09-08T12:00:00Z", "ENTRY", "GFGC6600OC", "weekly_asymmetric", 8.0, 8.0, position_id="p1"),
        _row("2026-09-11T12:00:00Z", "CLOSE", "GFGC6600OC", "weekly_asymmetric", -8.0, 0.0, position_id="p1"),
        _row("2026-09-30T17:52:07Z", "PARTIAL_EXIT", "GFGC6600OC", "weekly_asymmetric", -4.0, 4.0, position_id="p2"),
    ])
    violations = check_cumulative_long_only_ledger(df)
    assert len(violations) == 1
    v = violations[0]
    assert v.check == "cumulative_long_only_ledger"
    assert v.severity == "CRITICAL"
    assert "GFGC6600OC" in v.detail
    assert "gap real" in v.detail


def test_cumulative_ledger_marks_info_when_journal_starts_already_negative():
    """
    Patron de los 6 simbolos "unknown_legacy" reales (GFGC6800OC y otros):
    la PRIMERA fila que existe en el journal para ese (symbol, strategy_tag)
    ya arranca en negativo - consistente con fills anteriores al inicio
    real del journal (hueco de cobertura ya documentado), no un gap de
    logging nuevo. Debe marcarse INFO, nunca ocultarse ni subirse a
    CRITICAL.
    """
    df = _df([
        _row("2026-09-10T14:49:40Z", "PARTIAL_EXIT", "GFGC6800OC", "weekly_asymmetric", -1.0, 3.0, position_id="p1"),
    ])
    violations = check_cumulative_long_only_ledger(df)
    assert len(violations) == 1
    v = violations[0]
    assert v.severity == "INFO"
    assert "hueco de cobertura ya documentado" in v.detail


def test_cumulative_ledger_excludes_spread_completion_wing():
    """
    El mismo caso de GFGV5000OC debe quedar excluido tambien de este check
    (no solo de check_long_only): si se incluyera la pata ENTRY
    reason="spread_completion", el acumulado arrancaria en -75 y dispararia
    un falso positivo igual de invalido que el de check_long_only.
    """
    df = _df([
        _row("2026-10-01T10:47:59Z", "ENTRY", "GFGV5000OC", "weekly_asymmetric", -75.0, -75.0,
             position_id="ef268fed9a99", reason="spread_completion"),
        _row("2026-10-02T18:01:45Z", "CLOSE", "GFGV5000OC", "weekly_asymmetric", 75.0, 0.0,
             position_id="ef268fed9a99", reason="manual_close"),
    ])
    violations = check_cumulative_long_only_ledger(df)
    assert violations == []


def test_cumulative_ledger_ignores_non_long_only_strategies():
    """delta_hedge (GGAL underlying, el "fantasma de la era mock") no es
    long-only - este check no debe opinar sobre el, igual que check_long_only."""
    df = _df([
        _row("2026-08-01T12:00:00Z", "ENTRY", "GGAL", "delta_hedge", -335.85, -335.85, reason="hedge"),
    ])
    violations = check_cumulative_long_only_ledger(df)
    assert violations == []


def test_cumulative_ledger_does_not_flag_a_clean_long_only_cycle():
    """Un ciclo ENTRY->PARTIAL_EXIT->CLOSE normal, sin ningun hueco, nunca debe marcarse."""
    df = _df([
        _row("2026-09-08T12:00:00Z", "ENTRY", "GFGC9000OC", "weekly_asymmetric", 10.0, 10.0, position_id="p1"),
        _row("2026-09-09T12:00:00Z", "PARTIAL_EXIT", "GFGC9000OC", "weekly_asymmetric", -6.0, 4.0, position_id="p1"),
        _row("2026-09-10T12:00:00Z", "CLOSE", "GFGC9000OC", "weekly_asymmetric", -4.0, 0.0, position_id="p1"),
    ])
    violations = check_cumulative_long_only_ledger(df)
    assert violations == []


def test_cumulative_ledger_ignores_rows_with_missing_quantity_delta():
    """quantity_delta vacio/NaN (ej. REJECT) nunca se fabrica como 0 - se saltea esa fila sin romper el acumulado."""
    df = _df([
        _row("2026-09-08T12:00:00Z", "ENTRY", "GFGC9100OC", "weekly_asymmetric", 5.0, 5.0, position_id="p1"),
        _row("2026-09-09T12:00:00Z", "REJECT", "GFGC9100OC", "weekly_asymmetric", None, None, position_id="p1"),
        _row("2026-09-10T12:00:00Z", "CLOSE", "GFGC9100OC", "weekly_asymmetric", -5.0, 0.0, position_id="p1"),
    ])
    df["quantity_delta"] = pd.to_numeric(df["quantity_delta"], errors="coerce")
    violations = check_cumulative_long_only_ledger(df)
    assert violations == []


# --- integracion: run_audit end-to-end sobre un journal sintetico ---

def test_run_audit_integration_critical_gap_fails_exit_code(tmp_path):
    csv_path = tmp_path / "position_events.csv"
    df = _df([
        _row("2026-09-08T12:00:00Z", "ENTRY", "GFGC6600OC", "weekly_asymmetric", 8.0, 8.0, position_id="p1"),
        _row("2026-09-11T12:00:00Z", "CLOSE", "GFGC6600OC", "weekly_asymmetric", -8.0, 0.0, position_id="p1"),
        _row("2026-09-30T17:52:07Z", "PARTIAL_EXIT", "GFGC6600OC", "weekly_asymmetric", -4.0, 4.0, position_id="p2"),
    ])
    df.to_csv(csv_path, index=False)
    exit_code = run_audit(str(csv_path))
    assert exit_code == 1  # CRITICAL hace fallar el exit code


def test_run_audit_integration_clean_journal_passes():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "position_events.csv")
        df = _df([
            _row("2026-09-08T12:00:00Z", "ENTRY", "GFGC9000OC", "weekly_asymmetric", 10.0, 10.0, position_id="p1"),
            _row("2026-09-10T12:00:00Z", "CLOSE", "GFGC9000OC", "weekly_asymmetric", -10.0, 0.0, position_id="p1"),
        ])
        df.to_csv(csv_path, index=False)
        exit_code = run_audit(csv_path)
        assert exit_code == 0


ALL_TESTS = [
    test_check_long_only_flags_real_naked_short,
    test_check_long_only_excludes_spread_completion_wing,
    test_check_long_only_ignores_non_long_only_strategies,
    test_cumulative_ledger_flags_critical_when_real_gap_after_covering_zero,
    test_cumulative_ledger_marks_info_when_journal_starts_already_negative,
    test_cumulative_ledger_excludes_spread_completion_wing,
    test_cumulative_ledger_ignores_non_long_only_strategies,
    test_cumulative_ledger_does_not_flag_a_clean_long_only_cycle,
    test_cumulative_ledger_ignores_rows_with_missing_quantity_delta,
    test_run_audit_integration_clean_journal_passes,
]


if __name__ == "__main__":
    import tempfile as _tempfile
    from pathlib import Path as _Path

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

    # test_run_audit_integration_critical_gap_fails_exit_code necesita el
    # fixture tmp_path de pytest - se corre aparte con un TemporaryDirectory
    # equivalente cuando se ejecuta este archivo standalone (no via pytest).
    try:
        with _tempfile.TemporaryDirectory() as tmp:
            test_run_audit_integration_critical_gap_fails_exit_code(_Path(tmp))
        print("OK   - test_run_audit_integration_critical_gap_fails_exit_code")
    except AssertionError as exc:
        failures += 1
        print(f"FAIL - test_run_audit_integration_critical_gap_fails_exit_code: {exc}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"ERROR - test_run_audit_integration_critical_gap_fails_exit_code: {exc!r}")

    total = len(ALL_TESTS) + 1
    print(f"\n{total - failures}/{total} tests OK")
    if failures:
        raise SystemExit(1)
