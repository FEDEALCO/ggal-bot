"""
test_dashboard_data_ccl_bonds.py
===================================
Tests para dashboard/data/ccl_bonds.py (loader + calculo de CCL implicito
a partir de cotizaciones RAW de bonos - MEJORA 2026-09-30, ver REPORT.md).

Correr con:
    python -m ggal_bot.validation.test_dashboard_data_ccl_bonds
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dashboard.data import ccl_bonds as cb


def _write_csv(rows) -> Path:
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    path = Path(name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cb.CCL_BOND_QUOTE_COLUMNS)
        w.writerows(rows)
    return path


def _missing_path() -> Path:
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    path = Path(name)
    path.unlink()
    return path


def test_load_ccl_bond_quotes_empty_when_file_missing():
    df = cb.load_ccl_bond_quotes(_missing_path())
    assert df.empty
    assert list(df.columns) == cb.CCL_BOND_QUOTE_COLUMNS


def test_compute_ccl_series_computes_mid_ratio_for_matching_timestamps():
    path = _write_csv([
        ["2026-09-30T15:00:00+00:00", "GD30", "87500.0", "87600.0", "87540.0", "10", "10"],
        ["2026-09-30T15:00:00+00:00", "GD30C", "54.10", "54.20", "54.15", "5", "5"],
    ])
    try:
        df = cb.load_ccl_bond_quotes(path)
        series = cb.compute_ccl_series(df, pair=("GD30", "GD30C"))
        assert len(series) == 1
        # mid_ars = 87550.0, mid_usd = 54.15 -> ccl ~ 1616.8
        expected = 87550.0 / 54.15
        assert abs(series.iloc[0]["ccl"] - expected) < 1e-6
    finally:
        path.unlink(missing_ok=True)


def test_compute_ccl_series_excludes_timestamps_missing_one_leg():
    path = _write_csv([
        ["2026-09-30T15:00:00+00:00", "GD30", "87500.0", "87600.0", "87540.0", "10", "10"],
        # GD30C nunca aparece en este timestamp - nunca se fabrica el punto
        ["2026-09-30T15:05:00+00:00", "GD30", "87600.0", "87700.0", "87650.0", "10", "10"],
        ["2026-09-30T15:05:00+00:00", "GD30C", "54.20", "54.30", "54.25", "5", "5"],
    ])
    try:
        df = cb.load_ccl_bond_quotes(path)
        series = cb.compute_ccl_series(df, pair=("GD30", "GD30C"))
        assert len(series) == 1  # solo el timestamp con ambas patas
    finally:
        path.unlink(missing_ok=True)


def test_compute_ccl_series_excludes_row_with_blank_bid_or_ask():
    path = _write_csv([
        ["2026-09-30T15:00:00+00:00", "GD30", "", "87600.0", "87540.0", "10", "10"],  # bid faltante
        ["2026-09-30T15:00:00+00:00", "GD30C", "54.10", "54.20", "54.15", "5", "5"],
    ])
    try:
        df = cb.load_ccl_bond_quotes(path)
        series = cb.compute_ccl_series(df, pair=("GD30", "GD30C"))
        assert series.empty
    finally:
        path.unlink(missing_ok=True)


def test_compute_ccl_series_empty_when_dataframe_empty():
    df = cb.load_ccl_bond_quotes(_missing_path())
    series = cb.compute_ccl_series(df)
    assert series.empty


def test_get_latest_ccl_returns_none_when_series_empty():
    df = cb.load_ccl_bond_quotes(_missing_path())
    assert cb.get_latest_ccl(df) is None


def test_get_latest_ccl_returns_last_point_of_series():
    path = _write_csv([
        ["2026-09-30T15:00:00+00:00", "GD30", "87500.0", "87600.0", "87540.0", "10", "10"],
        ["2026-09-30T15:00:00+00:00", "GD30C", "54.10", "54.20", "54.15", "5", "5"],
        ["2026-09-30T15:05:00+00:00", "GD30", "87600.0", "87700.0", "87650.0", "10", "10"],
        ["2026-09-30T15:05:00+00:00", "GD30C", "54.20", "54.30", "54.25", "5", "5"],
    ])
    try:
        df = cb.load_ccl_bond_quotes(path)
        latest = cb.get_latest_ccl(df, pair=("GD30", "GD30C"))
        expected = ((87600.0 + 87700.0) / 2.0) / ((54.20 + 54.30) / 2.0)
        assert latest is not None and abs(latest - expected) < 1e-6
    finally:
        path.unlink(missing_ok=True)


def test_supports_al30_pair_independently_of_gd30():
    path = _write_csv([
        ["2026-09-30T15:00:00+00:00", "AL30", "83900.0", "84000.0", "83950.0", "8", "8"],
        ["2026-09-30T15:00:00+00:00", "AL30C", "51.80", "51.95", "51.89", "3", "3"],
    ])
    try:
        df = cb.load_ccl_bond_quotes(path)
        series = cb.compute_ccl_series(df, pair=("AL30", "AL30C"))
        assert len(series) == 1
    finally:
        path.unlink(missing_ok=True)


ALL_TESTS = [
    test_load_ccl_bond_quotes_empty_when_file_missing,
    test_compute_ccl_series_computes_mid_ratio_for_matching_timestamps,
    test_compute_ccl_series_excludes_timestamps_missing_one_leg,
    test_compute_ccl_series_excludes_row_with_blank_bid_or_ask,
    test_compute_ccl_series_empty_when_dataframe_empty,
    test_get_latest_ccl_returns_none_when_series_empty,
    test_get_latest_ccl_returns_last_point_of_series,
    test_supports_al30_pair_independently_of_gd30,
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
