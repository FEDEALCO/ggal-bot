"""
test_dashboard_pnl.py
========================
Tests de sanity para dashboard/pnl_engine.py (apareo FIFO de compras/ventas,
marca a mercado de posiciones abiertas, metricas de portafolio). No
requiere streamlit ni plotly - solo pandas/numpy (ya usados por el motor).
Correr con:

    python -m ggal_bot.validation.test_dashboard_pnl
"""

from __future__ import annotations

import math
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd

from ggal_bot.config import SETTINGS
from dashboard import pnl_engine as pe


def _fill_row(ts, order_id, symbol, side, qty, price):
    return {
        "timestamp_utc": pd.Timestamp(ts, tz="UTC"), "client_order_id": order_id, "symbol": symbol,
        "side": side, "order_type": "limit", "quantity": qty, "requested_price": price,
        "fill_price": price, "reference_price": price, "event": "shadow_fill",
    }


def test_classify_strategy_uses_contado_and_futuro_tickers():
    cfg = SETTINGS.instruments
    assert pe.classify_strategy(cfg.contado_ticker) == "delta_hedge"
    if cfg.futuro_ticker:
        assert pe.classify_strategy(cfg.futuro_ticker) == "delta_hedge"
    assert pe.classify_strategy("GFGC5200O") == "vol_arbitrage"


def test_multiplier_for_symbol_is_1_for_underlying_and_option_multiplier_for_options():
    """
    Regresion del bug real reportado por el usuario: el dashboard mostraba
    un PnL Total de ~$1.670 millones cuando el CSV de fills solo sostenia
    unos pocos millones de PnL realizado. La causa era que match_trades_fifo()/
    mark_to_market() aplicaban el multiplicador de OPCIONES (100) tambien a
    las patas de delta-hedge sobre el subyacente (acciones, sin
    multiplicador de contrato) - inflando cada una de esas patas x100.
    """
    cfg = SETTINGS.instruments
    assert pe.multiplier_for_symbol(cfg.contado_ticker, option_multiplier=100.0) == 1.0
    if cfg.futuro_ticker:
        assert pe.multiplier_for_symbol(cfg.futuro_ticker, option_multiplier=100.0) == 1.0
    assert pe.multiplier_for_symbol("GFGC5200O", option_multiplier=100.0) == 100.0


def test_classify_and_multiplier_recognize_bare_underlying_symbol_alias():
    """
    Regresion del bug real de clasificacion (auditoria del 2026-08-27, ver
    docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.6): un fill que llega con
    el simbolo CORTO del subyacente ("GGAL" a secas, en vez del ticker
    completo calificado "MERV - XMEV - GGAL - 24hs") antes no matcheaba
    ninguna comparacion exacta de string, y caia al multiplicador de
    OPCIONES (100) y a la clasificacion "vol_arbitrage" en lugar de
    "delta_hedge" - reintroduciendo, para esa variante de simbolo, el mismo
    bug x100 que ya se habia corregido para el ticker canonico.
    """
    cfg = SETTINGS.instruments
    assert pe.classify_strategy(cfg.underlying_symbol) == "delta_hedge"
    assert pe.multiplier_for_symbol(cfg.underlying_symbol, option_multiplier=100.0) == 1.0
    assert pe.classify_option_type(cfg.underlying_symbol) == "subyacente"


def test_match_trades_fifo_uses_multiplier_1_for_delta_hedge_legs():
    """
    Reproduce el patron real del bug: un round-trip de delta-hedge sobre el
    subyacente (short 24 acciones a 6975, cubre a 6615.8332 - PnL real por
    accion, sin multiplicador de opciones) debe dar un PnL de ~$8,608, NO
    ~$860,800 (que es lo que daba antes de la correccion, x100 de mas).
    """
    contado = SETTINGS.instruments.contado_ticker
    fills = pd.DataFrame([
        _fill_row("2026-08-25T17:25:00Z", "h1", contado, "sell", 23.9635, 6975.0),
        _fill_row("2026-08-26T13:54:00Z", "h2", contado, "buy", 23.9635, 6615.8332),
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    assert len(open_lots) == 0
    assert len(closed) == 1
    trade = closed[0]
    assert trade.strategy == "delta_hedge"
    expected_pnl = (6975.0 - 6615.8332) * 23.9635  # multiplicador 1.0, NO 100.0
    assert abs(trade.pnl_ars - expected_pnl) < 1e-6
    assert trade.pnl_ars < 10_000.0  # el bug anterior daba ~$860,800 aca


def test_mark_to_market_uses_multiplier_1_for_delta_hedge_open_position():
    contado = SETTINGS.instruments.contado_ticker
    fills = pd.DataFrame([_fill_row("2026-08-26T10:00:00Z", "h3", contado, "buy", 10.0, 7000.0)])
    _, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    open_positions_df = pe.aggregate_open_positions(open_lots)

    bot_state = {"extra": {"spot_mid": 7100.0}}
    marked = pe.mark_to_market(open_positions_df, bot_state, option_multiplier=100.0)
    assert marked.iloc[0]["pnl_ars"] == (7100.0 - 7000.0) * 10.0  # = 1000.0, NO 100000.0


def test_summary_pnl_total_not_inflated_when_delta_hedge_and_options_mixed():
    """
    Escenario mixto (opciones + delta-hedge, como en produccion real):
    confirma que el PnL Total consolidado no arrastra la inflacion x100 en
    la parte de delta-hedge mientras las opciones si usan su multiplicador
    real de 100.
    """
    contado = SETTINGS.instruments.contado_ticker
    fills = pd.DataFrame([
        _fill_row("2026-01-01T10:00:00Z", "o1", "GFGC5200O", "buy", 1, 100.0),
        _fill_row("2026-01-01T10:05:00Z", "o2", "GFGC5200O", "sell", 1, 105.0),  # opcion: +500 (mult=100)
        _fill_row("2026-01-01T10:10:00Z", "d1", contado, "sell", 20.0, 7000.0),
        _fill_row("2026-01-01T10:15:00Z", "d2", contado, "buy", 20.0, 6900.0),  # subyacente: +2000 (mult=1)
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    open_marked = pe.mark_to_market(pe.aggregate_open_positions(open_lots), {}, option_multiplier=100.0)
    summary = pe.compute_summary(closed, open_marked)
    assert summary["pnl_realized_ars"] == 500.0 + 2000.0  # NO 500.0 + 200000.0


def test_match_trades_fifo_closes_simple_round_trip():
    fills = pd.DataFrame([
        _fill_row("2026-01-01T10:00:00Z", "a1", "GFGC5200O", "buy", 1, 100.0),
        _fill_row("2026-01-01T10:05:00Z", "a2", "GFGC5200O", "sell", 1, 110.0),
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    assert len(open_lots) == 0
    assert len(closed) == 1
    trade = closed[0]
    assert trade.direction == "long"
    assert trade.quantity == 1
    assert trade.pnl_ars == (110.0 - 100.0) * 1 * 100.0  # = 1000.0


def test_match_trades_fifo_handles_short_round_trip():
    fills = pd.DataFrame([
        _fill_row("2026-01-01T10:00:00Z", "s1", "GFGV4800O", "sell", 2, 50.0),
        _fill_row("2026-01-01T10:10:00Z", "s2", "GFGV4800O", "buy", 2, 40.0),
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    assert len(open_lots) == 0
    assert len(closed) == 1
    trade = closed[0]
    assert trade.direction == "short"
    # Short: gana cuando el precio de recompra es MENOR al de venta.
    assert trade.pnl_ars == (50.0 - 40.0) * 2 * 100.0  # = 2000.0


def test_match_trades_fifo_partial_close_leaves_open_remainder():
    fills = pd.DataFrame([
        _fill_row("2026-01-01T10:00:00Z", "b1", "GFGC5200O", "buy", 3, 100.0),
        _fill_row("2026-01-01T10:05:00Z", "b2", "GFGC5200O", "sell", 1, 120.0),
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    assert len(closed) == 1
    assert closed[0].quantity == 1
    assert len(open_lots) == 1
    assert open_lots[0].quantity == 2  # 3 compradas - 1 vendida = 2 todavia abiertas
    assert open_lots[0].entry_price == 100.0


def test_match_trades_fifo_reproduces_the_reported_reentry_bug_pattern_correctly():
    """
    Regresion indirecta del bug de reentrada reportado antes (ver
    run_bot._act_on_signal): si el bot HUBIESE seguido comprando la misma
    base repetidamente sin cerrar nunca, este motor debe reflejar eso como
    una posicion abierta que CRECE (no debe inventar cierres que no
    ocurrieron - todos los fills son compras, nunca hay venta que aparee).
    """
    fills = pd.DataFrame([
        _fill_row(f"2026-01-01T10:0{i}:00Z", f"r{i}", "GFGC8000OC", "buy", 1, 192.0 + i)
        for i in range(5)
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    assert len(closed) == 0  # nunca hubo una venta que apareara: nada se "cerro" de la nada
    # match_trades_fifo guarda un lote separado por cada compra sin aparear
    # (FIFO puro); aggregate_open_positions es quien consolida la posicion
    # visible en el dashboard - ahi si debe verse como una sola fila creciendo.
    aggregated = pe.aggregate_open_positions(open_lots)
    assert len(aggregated) == 1
    assert aggregated.iloc[0]["quantity"] == 5


def test_aggregate_open_positions_weighted_average_price():
    fills = pd.DataFrame([
        _fill_row("2026-01-01T10:00:00Z", "w1", "GFGC5200O", "buy", 1, 100.0),
        _fill_row("2026-01-01T10:01:00Z", "w2", "GFGC5200O", "buy", 1, 120.0),
    ])
    _, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    aggregated = pe.aggregate_open_positions(open_lots)
    assert len(aggregated) == 1
    assert aggregated.iloc[0]["quantity"] == 2
    assert aggregated.iloc[0]["avg_entry_price"] == 110.0  # promedio simple porque las cantidades son iguales


def test_mark_to_market_computes_unrealized_pnl_from_bot_state():
    fills = pd.DataFrame([_fill_row("2026-01-01T10:00:00Z", "m1", "GFGC5200O", "buy", 1, 100.0)])
    _, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    open_positions_df = pe.aggregate_open_positions(open_lots)

    bot_state = {"option_chain_snapshot": [{"symbol": "GFGC5200O", "mid": 115.0}]}
    marked = pe.mark_to_market(open_positions_df, bot_state, option_multiplier=100.0)
    assert marked.iloc[0]["has_current_price"] == True  # noqa: E712
    assert marked.iloc[0]["pnl_ars"] == (115.0 - 100.0) * 1 * 100.0  # = 1500.0


def test_mark_to_market_flags_missing_price_as_zero_pnl():
    fills = pd.DataFrame([_fill_row("2026-01-01T10:00:00Z", "m2", "GFGC9999O", "buy", 1, 100.0)])
    _, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    open_positions_df = pe.aggregate_open_positions(open_lots)

    marked = pe.mark_to_market(open_positions_df, bot_state={}, option_multiplier=100.0)
    assert marked.iloc[0]["has_current_price"] == False  # noqa: E712
    assert marked.iloc[0]["pnl_ars"] == 0.0

    # Regresion de un bug real reportado por el usuario: dashboard/app.py
    # llama a .round(4) sobre la columna current_price para mostrarla en la
    # tabla de "Abiertas". Si get_current_price() devuelve None y esa columna
    # queda en dtype object (Nones sueltos en vez de NaN), pandas explota con
    # "TypeError: type NoneType doesn't define __round__ method". Confirmar
    # que la columna es realmente numerica (float, con NaN) para que el
    # redondeo sea seguro sin importar si falta la cotizacion.
    assert pd.api.types.is_float_dtype(marked["current_price"])
    marked["current_price"].round(4)  # no debe lanzar


def test_compute_summary_win_rate_and_profit_factor():
    fills = pd.DataFrame([
        _fill_row("2026-01-01T10:00:00Z", "p1", "GFGC5200O", "buy", 1, 100.0),
        _fill_row("2026-01-01T10:05:00Z", "p2", "GFGC5200O", "sell", 1, 120.0),  # +2000
        _fill_row("2026-01-01T10:10:00Z", "p3", "GFGC6000O", "buy", 1, 200.0),
        _fill_row("2026-01-01T10:15:00Z", "p4", "GFGC6000O", "sell", 1, 190.0),  # -1000
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    open_marked = pe.mark_to_market(pe.aggregate_open_positions(open_lots), {}, option_multiplier=100.0)
    summary = pe.compute_summary(closed, open_marked)

    assert summary["n_closed_trades"] == 2
    assert summary["win_rate_pct"] == 50.0
    assert summary["pnl_realized_ars"] == 1000.0  # +2000 - 1000
    assert summary["profit_factor"] == 2.0  # 2000 ganancia bruta / 1000 perdida bruta


def test_compute_max_drawdown_on_synthetic_equity_curve():
    fills = pd.DataFrame([
        _fill_row("2026-01-01T10:00:00Z", "d1", "A", "buy", 1, 100.0),
        _fill_row("2026-01-01T10:01:00Z", "d2", "A", "sell", 1, 150.0),   # +5000 (equity: 5000)
        _fill_row("2026-01-01T10:02:00Z", "d3", "B", "buy", 1, 100.0),
        _fill_row("2026-01-01T10:03:00Z", "d4", "B", "sell", 1, 80.0),    # -2000 (equity: 3000)
    ])
    closed, _ = pe.match_trades_fifo(fills, option_multiplier=100.0)
    equity_curve = pe.compute_equity_curve(closed)
    dd = pe.compute_max_drawdown(equity_curve)
    assert dd["max_drawdown_ars"] == -2000.0
    assert abs(dd["max_drawdown_pct"] - (-40.0)) < 1e-6  # -2000 / 5000 pico


def test_load_fills_returns_empty_frame_when_file_missing(tmp_path=None):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp_dir:
        missing_path = Path(tmp_dir) / "no_existe.csv"
        df = pe.load_fills(csv_path=missing_path)
        assert df.empty


def test_fit_smile_curve_returns_quadratic_shape():
    df = pd.DataFrame([
        {"strike": 5000.0, "spot_ref": 5200.0, "iv": 0.60},
        {"strike": 5200.0, "spot_ref": 5200.0, "iv": 0.55},
        {"strike": 5400.0, "spot_ref": 5200.0, "iv": 0.58},
        {"strike": 4800.0, "spot_ref": 5200.0, "iv": 0.65},
    ])
    curve = pe.fit_smile_curve(df, n_points=20)
    assert len(curve) == 20
    assert curve["fitted_iv"].min() > 0  # sonrisa razonable, sin IVs negativas en el rango ajustado


ALL_TESTS = [
    test_classify_strategy_uses_contado_and_futuro_tickers,
    test_multiplier_for_symbol_is_1_for_underlying_and_option_multiplier_for_options,
    test_classify_and_multiplier_recognize_bare_underlying_symbol_alias,
    test_match_trades_fifo_uses_multiplier_1_for_delta_hedge_legs,
    test_mark_to_market_uses_multiplier_1_for_delta_hedge_open_position,
    test_summary_pnl_total_not_inflated_when_delta_hedge_and_options_mixed,
    test_match_trades_fifo_closes_simple_round_trip,
    test_match_trades_fifo_handles_short_round_trip,
    test_match_trades_fifo_partial_close_leaves_open_remainder,
    test_match_trades_fifo_reproduces_the_reported_reentry_bug_pattern_correctly,
    test_aggregate_open_positions_weighted_average_price,
    test_mark_to_market_computes_unrealized_pnl_from_bot_state,
    test_mark_to_market_flags_missing_price_as_zero_pnl,
    test_compute_summary_win_rate_and_profit_factor,
    test_compute_max_drawdown_on_synthetic_equity_curve,
    test_load_fills_returns_empty_frame_when_file_missing,
    test_fit_smile_curve_returns_quadratic_shape,
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
