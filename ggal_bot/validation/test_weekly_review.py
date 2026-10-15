"""
test_weekly_review.py
=======================
Tests para ggal_bot/ops/weekly_review.py (revision semanal en un comando,
2026-10-15, a pedido explicito del usuario - ver docstring del modulo y
FREEZE.md). Todo sobre fixtures sinteticas en un directorio temporal
(nunca contra los logs reales de produccion, ni siquiera de lectura) -
mismo criterio que el resto de ggal_bot/validation/.

Correr con:
    python -m pytest ggal_bot/validation/test_weekly_review.py
"""
from __future__ import annotations

import csv
import gzip
import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.ops import weekly_review as wr

_JOURNAL_FIELDS = [
    "timestamp_utc", "event_type", "position_id", "contract_key", "symbol",
    "strategy_tag", "side", "quantity_delta", "quantity_after", "price",
    "order_client_id", "reason", "data_unavailable_fields",
]

_SNAPSHOT_FIELDS = [
    "timestamp_utc", "symbol", "option_type", "strike", "expiry",
    "days_calendar", "days_business", "spot_ref", "bid", "ask",
    "bid_size", "ask_size", "iv", "delta", "gamma", "vega", "theta",
    "last_price", "last_volume",
]

_FILL_FIELDS = [
    "timestamp_utc", "client_order_id", "symbol", "side", "order_type",
    "quantity", "requested_price", "fill_price", "reference_price", "event",
    "bid_at_fill", "ask_at_fill", "mid_at_fill",
]


def _write_csv(path: Path, fields, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def _journal_row(event_type, pid, symbol="GFGC6600OC", strategy_tag="weekly_asymmetric",
                  qty_delta=5.0, qty_after=5.0, price=100.0, ts="2026-10-13T14:00:00+00:00",
                  side="buy", reason="", contract_key="GGAL|GFGC6600OC|2026-10-16"):
    return {
        "timestamp_utc": ts, "event_type": event_type, "position_id": pid,
        "contract_key": contract_key, "symbol": symbol, "strategy_tag": strategy_tag,
        "side": side, "quantity_delta": qty_delta, "quantity_after": qty_after,
        "price": price, "order_client_id": f"cid-{pid}", "reason": reason,
        "data_unavailable_fields": "",
    }


# ---------------------------------------------------------------------------
# load_position_events_rows / section_trades_pnl_by_strategy
# ---------------------------------------------------------------------------

def test_load_position_events_rows_missing_file_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        assert wr.load_position_events_rows(Path(d)) == []


def test_load_position_events_rows_sorted_chronologically():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        rows = [
            _journal_row("CLOSE", "p1", qty_delta=-5.0, qty_after=0.0, price=150.0, ts="2026-10-13T15:00:00+00:00"),
            _journal_row("ENTRY", "p1", ts="2026-10-13T14:00:00+00:00"),
        ]
        _write_csv(logs_dir / "position_events.csv", _JOURNAL_FIELDS, rows)
        loaded = wr.load_position_events_rows(logs_dir)
        assert [r["event_type"] for r in loaded] == ["ENTRY", "CLOSE"]


def test_section_trades_pnl_by_strategy_reports_closed_trade():
    rows = [
        _journal_row("ENTRY", "p1", qty_delta=5.0, qty_after=5.0, price=100.0, ts="2026-10-13T14:00:00+00:00"),
        _journal_row("CLOSE", "p1", qty_delta=-5.0, qty_after=0.0, price=150.0, ts="2026-10-13T15:00:00+00:00", side="sell", reason="take_profit"),
    ]
    out = wr.section_trades_pnl_by_strategy(rows)
    assert "weekly_asymmetric" in out
    assert "1" in out  # n_trades == 1 en algun lado de la fila


def test_section_trades_pnl_by_strategy_empty_journal_is_data_insufficient():
    assert "DATA INSUFFICIENT" in wr.section_trades_pnl_by_strategy([])


# ---------------------------------------------------------------------------
# section_invariant_violations
# ---------------------------------------------------------------------------

def test_section_invariant_violations_ok_on_clean_journal():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        rows = [
            _journal_row("ENTRY", "p1", qty_delta=5.0, qty_after=5.0, price=100.0, ts="2026-10-13T14:00:00+00:00"),
            _journal_row("CLOSE", "p1", qty_delta=-5.0, qty_after=0.0, price=150.0, ts="2026-10-13T15:00:00+00:00", side="sell"),
        ]
        _write_csv(logs_dir / "position_events.csv", _JOURNAL_FIELDS, rows)
        out = wr.section_invariant_violations(logs_dir)
        assert out.startswith("OK")


def test_section_invariant_violations_flags_naked_short_long_only():
    """Un weekly_asymmetric que abre NETO corto (sin spread_completion) rompe check_long_only."""
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        rows = [
            _journal_row("ENTRY", "p1", qty_delta=-5.0, qty_after=-5.0, price=100.0, ts="2026-10-13T14:00:00+00:00", side="sell"),
        ]
        _write_csv(logs_dir / "position_events.csv", _JOURNAL_FIELDS, rows)
        out = wr.section_invariant_violations(logs_dir)
        assert "violaciones" in out
        assert "long_only" in out


def test_section_invariant_violations_missing_file_is_data_insufficient():
    with tempfile.TemporaryDirectory() as d:
        assert "DATA INSUFFICIENT" in wr.section_invariant_violations(Path(d))


# ---------------------------------------------------------------------------
# load_market_snapshot_rows (vivo + rotados .gz)
# ---------------------------------------------------------------------------

def test_load_market_snapshot_rows_combines_live_and_gz_siblings():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        live_row = {k: "" for k in _SNAPSHOT_FIELDS}
        live_row.update(symbol="GFGC6600OC", bid="100", ask="105", last_price="102", timestamp_utc="2026-10-14T14:00:00+00:00")
        _write_csv(logs_dir / "market_snapshots.csv", _SNAPSHOT_FIELDS, [live_row])

        gz_row = dict(live_row)
        gz_row.update(timestamp_utc="2026-10-13T14:00:00+00:00", last_price="0")
        gz_path = logs_dir / "market_snapshots-2026-10-13.csv.gz"
        with gzip.open(gz_path, "wt", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_SNAPSHOT_FIELDS)
            writer.writeheader()
            writer.writerow(gz_row)

        rows = wr.load_market_snapshot_rows(logs_dir)
        assert len(rows) == 2
        timestamps = {r["timestamp_utc"] for r in rows}
        assert timestamps == {"2026-10-14T14:00:00+00:00", "2026-10-13T14:00:00+00:00"}


def test_load_market_snapshot_rows_missing_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        assert wr.load_market_snapshot_rows(Path(d)) == []


# ---------------------------------------------------------------------------
# section_valid_quote_pct_per_traded_symbol
# ---------------------------------------------------------------------------

def test_valid_quote_pct_counts_tight_and_wide_spreads_correctly():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        journal_rows = [_journal_row("ENTRY", "p1", symbol="GFGC6600OC")]

        def _snap(bid, ask):
            row = {k: "" for k in _SNAPSHOT_FIELDS}
            row.update(symbol="GFGC6600OC", bid=str(bid), ask=str(ask), timestamp_utc="2026-10-13T14:00:00+00:00")
            return row

        # spread relativo tight = (105-100)/102.5 = 4.9% <= 5% default -> valida
        # spread relativo wide = (150-100)/125 = 40.0% -> invalida
        rows = [_snap(100, 105), _snap(100, 150)]
        _write_csv(logs_dir / "market_snapshots.csv", _SNAPSHOT_FIELDS, rows)

        out = wr.section_valid_quote_pct_per_traded_symbol(logs_dir, journal_rows)
        assert "GFGC6600OC" in out
        assert "50.0%" in out  # 1 de 2 validas


def test_valid_quote_pct_reports_symbols_without_any_coverage():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        journal_rows = [_journal_row("ENTRY", "p1", symbol="GFGV5000OC")]
        row = {k: "" for k in _SNAPSHOT_FIELDS}
        row.update(symbol="OTHER_SYMBOL", bid="1", ask="1.1", timestamp_utc="2026-10-13T14:00:00+00:00")
        _write_csv(logs_dir / "market_snapshots.csv", _SNAPSHOT_FIELDS, [row])

        out = wr.section_valid_quote_pct_per_traded_symbol(logs_dir, journal_rows)
        assert "SIN NINGUNA fila" in out
        assert "GFGV5000OC" in out


def test_valid_quote_pct_no_traded_symbols_is_data_insufficient():
    with tempfile.TemporaryDirectory() as d:
        out = wr.section_valid_quote_pct_per_traded_symbol(Path(d), [])
        assert "DATA INSUFFICIENT" in out


# ---------------------------------------------------------------------------
# fills fuera de horario
# ---------------------------------------------------------------------------

def test_section_fills_outside_session_detects_weekend_fill():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        rows = [
            # 2026-10-13 es martes, 14:00 UTC = 11:00 ART -> dentro de rueda
            {k: "" for k in _FILL_FIELDS} | {
                "timestamp_utc": "2026-10-13T14:00:00+00:00", "symbol": "GFGC6600OC",
                "side": "buy", "quantity": "5", "event": "shadow_fill",
            },
            # 2026-10-11 es domingo -> fuera de rueda sin importar la hora
            {k: "" for k in _FILL_FIELDS} | {
                "timestamp_utc": "2026-10-11T14:00:00+00:00", "symbol": "GFGC6600OC",
                "side": "sell", "quantity": "5", "event": "shadow_fill",
            },
        ]
        _write_csv(logs_dir / "shadow_trades.csv", _FILL_FIELDS, rows)
        out = wr.section_fills_outside_session(logs_dir)
        assert "1/2 fills" in out
        assert "50.0%" in out


def test_section_fills_outside_session_missing_file_is_data_insufficient():
    with tempfile.TemporaryDirectory() as d:
        assert "DATA INSUFFICIENT" in wr.section_fills_outside_session(Path(d))


def test_load_shadow_fill_rows_ignores_non_fill_events():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        rows = [
            {k: "" for k in _FILL_FIELDS} | {"timestamp_utc": "2026-10-13T14:00:00+00:00", "event": "order_submitted"},
            {k: "" for k in _FILL_FIELDS} | {"timestamp_utc": "2026-10-13T14:00:01+00:00", "event": "shadow_fill"},
        ]
        _write_csv(logs_dir / "shadow_trades.csv", _FILL_FIELDS, rows)
        loaded = wr.load_shadow_fill_rows(logs_dir)
        assert len(loaded) == 1
        assert loaded[0]["event"] == "shadow_fill"


# ---------------------------------------------------------------------------
# backstop triggers
# ---------------------------------------------------------------------------

def test_backstop_trigger_parsing_from_log_lines():
    lines = [
        "2026-10-14 10:00:00 WARNING RESPALDO [catastrophic_loss_backstop] disparado para GFGC6800OC: resto del mensaje",
        "2026-10-14 10:05:00 WARNING RESPALDO [quote_gap_backstop] disparado para GFGV5000OC: resto del mensaje",
        "2026-10-14 10:06:00 INFO linea sin relacion",
    ]
    parsed = wr.parse_backstop_triggers(lines)
    assert parsed == [
        ("catastrophic_loss_backstop", "GFGC6800OC"),
        ("quote_gap_backstop", "GFGV5000OC"),
    ]


def test_section_backstop_triggers_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        log_text = (
            "2026-10-14 10:00:00 WARNING RESPALDO [catastrophic_loss_backstop] disparado para GFGC6800OC: x\n"
            "2026-10-14 10:01:00 INFO otra linea cualquiera\n"
        )
        (logs_dir / "ggal_bot.log").write_text(log_text, encoding="utf-8")
        out = wr.section_backstop_triggers(logs_dir)
        assert "1 disparos" in out
        assert "catastrophic_loss_backstop" in out


def test_section_backstop_triggers_missing_log_reports_zero():
    with tempfile.TemporaryDirectory() as d:
        out = wr.section_backstop_triggers(Path(d))
        assert out.startswith("0 disparos")


# ---------------------------------------------------------------------------
# H1 data accumulation
# ---------------------------------------------------------------------------

def test_h1_data_accumulation_counts_last_price_rows():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)

        def _snap(last_price, ts):
            row = {k: "" for k in _SNAPSHOT_FIELDS}
            row.update(symbol="GFGC6600OC", bid="100", ask="105", last_price=str(last_price), timestamp_utc=ts)
            return row

        rows = [
            _snap(0, "2026-10-13T14:00:00+00:00"),
            _snap(102.5, "2026-10-14T14:00:00+00:00"),
            _snap(103.0, "2026-10-14T15:00:00+00:00"),
        ]
        _write_csv(logs_dir / "market_snapshots.csv", _SNAPSHOT_FIELDS, rows)
        out = wr.section_h1_data_accumulation(logs_dir)
        assert "2/3 filas" in out
        assert "2026-10-14T14:00:00+00:00" in out


def test_h1_data_accumulation_no_rows_is_data_insufficient():
    with tempfile.TemporaryDirectory() as d:
        assert "DATA INSUFFICIENT" in wr.section_h1_data_accumulation(Path(d))


# ---------------------------------------------------------------------------
# build_report end-to-end (smoke test: no revienta con logs vacios o con datos)
# ---------------------------------------------------------------------------

def test_build_report_runs_end_to_end_on_empty_logs_dir():
    with tempfile.TemporaryDirectory() as d:
        report = wr.build_report(Path(d))
        assert "Revision semanal GGAL_BOT" in report
        assert "DATA INSUFFICIENT" in report  # todas las secciones sin datos


def test_build_report_runs_end_to_end_with_full_fixtures():
    with tempfile.TemporaryDirectory() as d:
        logs_dir = Path(d)
        journal_rows = [
            _journal_row("ENTRY", "p1", qty_delta=5.0, qty_after=5.0, price=100.0, ts="2026-10-13T14:00:00+00:00"),
            _journal_row("CLOSE", "p1", qty_delta=-5.0, qty_after=0.0, price=150.0, ts="2026-10-13T15:00:00+00:00", side="sell", reason="take_profit"),
        ]
        _write_csv(logs_dir / "position_events.csv", _JOURNAL_FIELDS, journal_rows)

        snap_row = {k: "" for k in _SNAPSHOT_FIELDS}
        snap_row.update(symbol="GFGC6600OC", bid="100", ask="105", last_price="102", timestamp_utc="2026-10-13T14:00:00+00:00")
        _write_csv(logs_dir / "market_snapshots.csv", _SNAPSHOT_FIELDS, [snap_row])

        fill_row = {k: "" for k in _FILL_FIELDS}
        fill_row.update(timestamp_utc="2026-10-13T14:00:00+00:00", symbol="GFGC6600OC", side="buy", quantity="5", event="shadow_fill")
        _write_csv(logs_dir / "shadow_trades.csv", _FILL_FIELDS, [fill_row])

        (logs_dir / "ggal_bot.log").write_text(
            "2026-10-13 10:00:00 WARNING RESPALDO [quote_gap_backstop] disparado para GFGC6600OC: x\n",
            encoding="utf-8",
        )

        report = wr.build_report(logs_dir)
        assert "weekly_asymmetric" in report
        assert "GFGC6600OC" in report
        assert "1 disparos" in report


ALL_TESTS = [
    test_load_position_events_rows_missing_file_returns_empty,
    test_load_position_events_rows_sorted_chronologically,
    test_section_trades_pnl_by_strategy_reports_closed_trade,
    test_section_trades_pnl_by_strategy_empty_journal_is_data_insufficient,
    test_section_invariant_violations_ok_on_clean_journal,
    test_section_invariant_violations_flags_naked_short_long_only,
    test_section_invariant_violations_missing_file_is_data_insufficient,
    test_load_market_snapshot_rows_combines_live_and_gz_siblings,
    test_load_market_snapshot_rows_missing_returns_empty,
    test_valid_quote_pct_counts_tight_and_wide_spreads_correctly,
    test_valid_quote_pct_reports_symbols_without_any_coverage,
    test_valid_quote_pct_no_traded_symbols_is_data_insufficient,
    test_section_fills_outside_session_detects_weekend_fill,
    test_section_fills_outside_session_missing_file_is_data_insufficient,
    test_load_shadow_fill_rows_ignores_non_fill_events,
    test_backstop_trigger_parsing_from_log_lines,
    test_section_backstop_triggers_end_to_end,
    test_section_backstop_triggers_missing_log_reports_zero,
    test_h1_data_accumulation_counts_last_price_rows,
    test_h1_data_accumulation_no_rows_is_data_insufficient,
    test_build_report_runs_end_to_end_on_empty_logs_dir,
    test_build_report_runs_end_to_end_with_full_fixtures,
]


if __name__ == "__main__":
    failures = 0
    for t in ALL_TESTS:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} tests OK")
    raise SystemExit(1 if failures else 0)
