"""
test_manual_close.py
=====================
Tests de ggal_bot/ops/manual_close.py (herramienta administrativa para
cerrar a mano una posicion SHADOW - ver docstring del modulo, pregunta del
usuario 2026-09-08 "COMO CIERRO MANUALMENTE LAS OPERACIONES?" sobre las 7
posiciones huerfanas de produccion).
"""
from __future__ import annotations

import csv as csv_module

import pytest

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.execution.order_gateway import OrderSide, ShadowAuditLogger
from ggal_bot.ops.manual_close import close_position_manually, determine_close_order

pytest.importorskip("pandas")


# Se reusa el HEADER real de ShadowAuditLogger (en vez de duplicarlo a mano)
# para que este fixture nunca vuelva a desincronizarse de el - BUG REAL
# EVITADO ACA (2026-10-01): una copia local desactualizada del header (10
# columnas, sin bid_at_fill/ask_at_fill/mid_at_fill) escribia una fila
# "jagged" apenas close_position_manually() agregaba su propio fill via
# ShadowAuditLogger (13 columnas) - pandas no pudo parsear el CSV resultante
# y load_fills() devolvia un DataFrame vacio en silencio.
_HEADER = ShadowAuditLogger._HEADER


# Timestamps con microsegundos (formato real de datetime.now(timezone.utc).
# isoformat(), ver ShadowAuditLogger.log_fill) - deliberadamente NO
# "2026-09-01T10:00:00+00:00" limpio, para no esconder el bug de
# pnl_engine.load_fills() con precision de sub-segundo mixta (ver
# test_load_fills_parses_mixed_subsecond_precision_timestamps_without_dropping_rows
# en test_dashboard_pnl.py) detras de timestamps de fixture poco realistas.


def _write_fill(path, *, symbol, side, quantity, price, ts):
    needs_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv_module.writer(f)
        if needs_header:
            writer.writerow(_HEADER)
        # bid_at_fill/ask_at_fill/mid_at_fill en blanco: estos fixtures
        # existian antes de esa mejora y no necesitan ese dato para lo que
        # este archivo testea (deteccion/cierre manual de posiciones).
        writer.writerow([ts, f"cid-{ts}", symbol, side, "limit", quantity, price, price, price, "shadow_fill", "", "", ""])


def test_determine_close_order_detects_sell_needed_for_a_long_option_position(tmp_path):
    csv_path = tmp_path / "shadow_trades.csv"
    _write_fill(csv_path, symbol="GFGC7600OC", side="buy", quantity=17.0, price=500.0, ts="2026-09-01T10:00:00.123456+00:00")

    result = determine_close_order("GFGC7600OC", csv_path=csv_path)
    assert result == (OrderSide.SELL, 17.0)


def test_determine_close_order_detects_buy_needed_for_a_short_underlying_position(tmp_path):
    csv_path = tmp_path / "shadow_trades.csv"
    _write_fill(
        csv_path, symbol="MERV - XMEV - GGAL - 24hs", side="sell", quantity=335.8504,
        price=7000.0, ts="2026-09-01T10:00:00.123456+00:00",
    )

    result = determine_close_order("MERV - XMEV - GGAL - 24hs", csv_path=csv_path)
    assert result is not None
    side, qty = result
    assert side == OrderSide.BUY
    assert qty == pytest.approx(335.8504)


def test_determine_close_order_returns_none_when_symbol_already_flat(tmp_path):
    csv_path = tmp_path / "shadow_trades.csv"
    _write_fill(csv_path, symbol="GFGC7000OC", side="buy", quantity=9.0, price=480.0, ts="2026-09-01T10:00:00.123456+00:00")
    _write_fill(csv_path, symbol="GFGC7000OC", side="sell", quantity=9.0, price=490.0, ts="2026-09-02T10:00:00.654321+00:00")

    assert determine_close_order("GFGC7000OC", csv_path=csv_path) is None


def test_close_position_manually_rejects_missing_or_zero_price(tmp_path):
    csv_path = tmp_path / "shadow_trades.csv"
    _write_fill(csv_path, symbol="GFGC7600OC", side="buy", quantity=17.0, price=500.0, ts="2026-09-01T10:00:00.123456+00:00")

    with pytest.raises(ValueError, match="precio de mercado real"):
        close_position_manually("GFGC7600OC", 0.0, csv_path=csv_path)
    with pytest.raises(ValueError, match="precio de mercado real"):
        close_position_manually("GFGC7600OC", None, csv_path=csv_path)


def test_close_position_manually_rejects_symbol_with_no_open_position(tmp_path):
    csv_path = tmp_path / "shadow_trades.csv"
    csv_path.write_text("")  # archivo vacio, ni siquiera header todavia

    with pytest.raises(ValueError, match="no se encontro una posicion abierta"):
        close_position_manually("GFGC7600OC", 612.5, csv_path=csv_path)


def test_close_position_manually_auto_detects_and_leaves_position_at_zero(tmp_path):
    from dashboard.pnl_engine import aggregate_open_positions, load_fills, match_trades_fifo

    csv_path = tmp_path / "shadow_trades.csv"
    _write_fill(csv_path, symbol="GFGC7600OC", side="buy", quantity=17.0, price=500.0, ts="2026-09-01T10:00:00.123456+00:00")

    side, qty = close_position_manually("GFGC7600OC", 612.5, csv_path=csv_path)
    assert side == OrderSide.SELL
    assert qty == 17.0

    fills = load_fills(csv_path)
    assert len(fills) == 2
    manual_row = fills.iloc[-1]
    assert manual_row["event"] == "shadow_fill"
    assert manual_row["side"] == "sell"
    assert manual_row["quantity"] == 17.0
    assert manual_row["fill_price"] == 612.5
    assert str(manual_row["client_order_id"]).startswith("manual-close-")

    _closed, open_lots = match_trades_fifo(fills)
    aggregated = aggregate_open_positions(open_lots)
    assert aggregated[aggregated["symbol"] == "GFGC7600OC"].empty


def test_close_position_manually_supports_explicit_partial_close(tmp_path):
    from dashboard.pnl_engine import aggregate_open_positions, match_trades_fifo, load_fills

    csv_path = tmp_path / "shadow_trades.csv"
    _write_fill(csv_path, symbol="GFGC7000OC", side="buy", quantity=9.0, price=480.0, ts="2026-09-01T10:00:00.123456+00:00")

    side, qty = close_position_manually(
        "GFGC7000OC", 500.0, quantity=4.0, side=OrderSide.SELL, csv_path=csv_path,
    )
    assert (side, qty) == (OrderSide.SELL, 4.0)

    fills = load_fills(csv_path)
    _closed, open_lots = match_trades_fifo(fills)
    aggregated = aggregate_open_positions(open_lots)
    row = aggregated[aggregated["symbol"] == "GFGC7000OC"]
    assert not row.empty
    assert row["quantity"].iloc[0] == pytest.approx(5.0)


def test_close_position_manually_rejects_explicit_quantity_that_would_go_net_short(tmp_path):
    """
    Tarea #27 item 4 (invariante 1, ggal_bot/risk/invariants.py): a
    diferencia de run_bot.py::_act_on_exit_signal (que siempre recorta de
    forma segura), esta herramienta escribe el fill tal cual se le pide -
    un --quantity explicito mayor a la posicion neta real dejaria una
    posicion NETA CORTA fabricada a mano. Debe rechazarse.
    """
    csv_path = tmp_path / "shadow_trades.csv"
    _write_fill(csv_path, symbol="GFGC7600OC", side="buy", quantity=17.0, price=500.0, ts="2026-09-01T10:00:00.123456+00:00")

    with pytest.raises(ValueError, match="posicion NETA CORTA"):
        close_position_manually(
            "GFGC7600OC", 612.5, quantity=25.0, side=OrderSide.SELL, csv_path=csv_path,
        )


def test_close_position_manually_rejects_sell_when_symbol_already_flat(tmp_path):
    csv_path = tmp_path / "shadow_trades.csv"
    csv_path.write_text("")

    with pytest.raises(ValueError, match="posicion NETA CORTA"):
        close_position_manually(
            "GFGC9999OC", 100.0, quantity=5.0, side=OrderSide.SELL, csv_path=csv_path,
        )


ALL_TESTS = [
    test_determine_close_order_detects_sell_needed_for_a_long_option_position,
    test_determine_close_order_detects_buy_needed_for_a_short_underlying_position,
    test_determine_close_order_returns_none_when_symbol_already_flat,
]


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    failures = 0
    for test_fn in ALL_TESTS:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                test_fn(Path(tmp))
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
