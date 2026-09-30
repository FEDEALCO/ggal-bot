"""
test_dashboard_data_reconciliation.py
========================================
Tests para dashboard/data/reconciliation.py (panel de reconciliacion,
Fase 1, mandato explicito del usuario 2026-09-30).

Correr con:
    python -m ggal_bot.validation.test_dashboard_data_reconciliation
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dashboard.data import journal as dj
from dashboard.data import reconciliation as rc

_RAW_HEADER = [
    "timestamp_utc", "event_type", "position_id", "contract_key", "symbol",
    "strategy_tag", "side", "quantity_delta", "quantity_after", "price",
    "order_client_id", "reason", "data_unavailable_fields",
]


def _write_raw_csv(rows) -> Path:
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    path = Path(name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_RAW_HEADER)
        w.writerows(rows)
    return path


def test_reconcile_closed_trades_by_strategy_sums_without_duplicates():
    path = _write_raw_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos1", "k", "GFGC5000O", "weekly_asymmetric", "buy", "10", "10", "100.0", "oc1", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "pos1", "k", "GFGC5000O", "weekly_asymmetric", "sell", "-10", "0", "110.0", "oc1", "r", ""],
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos2", "k2", "GFGV5000O", "scalping", "buy", "5", "5", "50.0", "oc2", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "pos2", "k2", "GFGV5000O", "scalping", "sell", "-5", "0", "60.0", "oc2", "r", ""],
    ])
    try:
        rows = dj.load_journal_rows(path)
        wk_trades, _, _ = dj.get_closed_trades(rows, strategies=("weekly_asymmetric",))
        sc_trades, _, _ = dj.get_closed_trades(rows, strategies=("scalping",))
        result = rc.reconcile_closed_trades_by_strategy({
            "weekly_asymmetric": wk_trades,
            "scalping": sc_trades,
        })
        assert result.is_reconciled is True
        assert result.duplicate_position_ids == []
        # weekly: (110-100)*10*100=10,000 | scalping: (60-50)*5*100=5,000
        assert abs(result.total_pnl_by_strategy_ars["weekly_asymmetric"] - 10_000.0) < 1e-6
        assert abs(result.total_pnl_by_strategy_ars["scalping"] - 5_000.0) < 1e-6
        assert abs(result.total_pnl_sum_ars - 15_000.0) < 1e-6
    finally:
        path.unlink(missing_ok=True)


def test_reconcile_closed_trades_by_strategy_flags_position_id_counted_twice():
    """
    Simula el tipo de bug que motivo este proyecto (classify_strategy()
    etiquetando todo como vol_arbitrage): el MISMO trade_id/position_id
    aparece en la lista de dos estrategias distintas -> debe detectarse,
    nunca sumarse en silencio.
    """
    path = _write_raw_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos1", "k", "GFGC5000O", "weekly_asymmetric", "buy", "10", "10", "100.0", "oc1", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "pos1", "k", "GFGC5000O", "weekly_asymmetric", "sell", "-10", "0", "110.0", "oc1", "r", ""],
    ])
    try:
        rows = dj.load_journal_rows(path)
        wk_trades, _, _ = dj.get_closed_trades(rows)  # sin filtro -> incluye pos1
        # Fabricamos artificialmente la condicion de bug en el test (nunca
        # en produccion): la MISMA lista de trades se cuenta bajo dos
        # "estrategias" distintas, como pasaria si un bug de clasificacion
        # duplicara el trade entre categorias.
        result = rc.reconcile_closed_trades_by_strategy({
            "weekly_asymmetric": wk_trades,
            "vol_arbitrage": wk_trades,
        })
        assert result.is_reconciled is False
        assert "pos1" in result.duplicate_position_ids
        assert result.detail is not None
    finally:
        path.unlink(missing_ok=True)


def test_cross_check_partition_consistent_on_real_data():
    path = _write_raw_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos_closed", "k", "GFGC5000O", "weekly_asymmetric", "buy", "10", "10", "100.0", "oc1", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "pos_closed", "k", "GFGC5000O", "weekly_asymmetric", "sell", "-10", "0", "110.0", "oc1", "r", ""],
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos_open", "k2", "GFGV5000O", "weekly_asymmetric", "buy", "10", "10", "50.0", "oc2", "e", ""],
    ])
    try:
        rows = dj.load_journal_rows(path)
        closed_result = dj.get_closed_trades(rows)
        open_result = dj.get_open_positions(rows)
        check = rc.cross_check_partition(rows, closed_result, open_result)
        assert check.is_consistent is True
        assert check.total_unique_position_ids == 2
        assert check.closed_trades_count == 1
        assert check.open_positions_count == 1
    finally:
        path.unlink(missing_ok=True)


def test_cross_check_partition_detects_inconsistency_when_results_come_from_different_filters():
    """
    Si por error el llamador pasa closed_result/open_result calculados con
    DISTINTOS filtros de estrategia (o sobre distintos rows), el cruce
    debe detectarlo, no reconciliar en silencio.
    """
    path = _write_raw_csv([
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos_closed", "k", "GFGC5000O", "weekly_asymmetric", "buy", "10", "10", "100.0", "oc1", "e", ""],
        ["2026-09-02T10:00:00+00:00", "CLOSE", "pos_closed", "k", "GFGC5000O", "weekly_asymmetric", "sell", "-10", "0", "110.0", "oc1", "r", ""],
        ["2026-09-01T10:00:00+00:00", "ENTRY", "pos_open", "k2", "GFGV5000O", "scalping", "buy", "10", "10", "50.0", "oc2", "e", ""],
    ])
    try:
        rows = dj.load_journal_rows(path)
        closed_result = dj.get_closed_trades(rows, strategies=("weekly_asymmetric",))
        # Mal uso deliberado: open_result viene de un filtro distinto (scalping)
        # en vez de sobre el mismo universo de rows/strategies.
        open_result = dj.get_open_positions(rows, strategies=("scalping",))
        check = rc.cross_check_partition(rows, closed_result, open_result)
        assert check.is_consistent is False
        assert check.detail is not None
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_reconcile_closed_trades_by_strategy_sums_without_duplicates,
    test_reconcile_closed_trades_by_strategy_flags_position_id_counted_twice,
    test_cross_check_partition_consistent_on_real_data,
    test_cross_check_partition_detects_inconsistency_when_results_come_from_different_filters,
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
