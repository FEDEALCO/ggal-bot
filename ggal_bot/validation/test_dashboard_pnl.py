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


def test_match_trades_fifo_uses_the_strategy_column_when_present():
    """
    BUG REAL ENCONTRADO Y CORREGIDO 2026-09-30 (al construir el panel de
    reconciliacion del dashboard - ver REPORT.md): match_trades_fifo()
    ignoraba una columna "strategy" ya presente en `fills` y volvia a
    calcular classify_strategy(symbol) por su cuenta (SIEMPRE
    "vol_arbitrage" para una opcion) - aunque dashboard/app.py ya hubiera
    poblado fills["strategy"] con el valor CORRECTO via
    classify_strategy_from_journal() antes de llamar a esta funcion. En
    los hechos, el fix de classify_strategy_from_journal (commit 4a64b22)
    nunca llegaba a la tabla "Cerradas"/"Abiertas" del dashboard: sin este
    test hubiera pasado desapercibido otra vez.
    """
    fills = pd.DataFrame([
        {**_fill_row("2026-09-10T10:00:00Z", "oc1", "GFGC5200O", "buy", 10, 100.0), "strategy": "weekly_asymmetric"},
        {**_fill_row("2026-09-11T10:00:00Z", "oc1", "GFGC5200O", "sell", 10, 120.0), "strategy": "weekly_asymmetric"},
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    assert len(closed) == 1
    assert closed[0].strategy == "weekly_asymmetric"  # NUNCA "vol_arbitrage" cuando la columna ya viene clasificada


def test_match_trades_fifo_falls_back_to_classify_strategy_when_column_absent():
    """
    Compatibilidad hacia atras: un llamador que arma `fills` sin columna
    "strategy" (como el resto de los tests de este archivo, y cualquier
    uso historico de esta funcion) debe seguir viendo el comportamiento de
    siempre - classify_strategy(symbol) por simbolo.
    """
    fills = pd.DataFrame([
        _fill_row("2026-09-10T10:00:00Z", "oc1", "GFGC5200O", "buy", 10, 100.0),
        _fill_row("2026-09-11T10:00:00Z", "oc1", "GFGC5200O", "sell", 10, 120.0),
    ])
    assert "strategy" not in fills.columns
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    assert closed[0].strategy == "vol_arbitrage"  # fallback historico, sin columna no hay otra fuente


def test_aggregate_open_positions_also_uses_the_strategy_column_when_present():
    """Mismo bug/fix que arriba, para el lado de posiciones ABIERTAS (OpenLot.strategy viene de la misma variable local)."""
    fills = pd.DataFrame([
        {**_fill_row("2026-09-10T10:00:00Z", "oc1", "GFGC5200O", "buy", 10, 100.0), "strategy": "scalping"},
    ])
    _, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    assert len(open_lots) == 1
    assert open_lots[0].strategy == "scalping"


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


def _event_row(order_client_id, strategy_tag, event_type="CLOSE", symbol="GFGC5200O"):
    return {
        "timestamp_utc": pd.Timestamp("2026-09-10T10:00:00Z", tz="UTC"), "event_type": event_type,
        "position_id": f"pos-{order_client_id}", "contract_key": symbol, "symbol": symbol,
        "strategy_tag": strategy_tag, "side": "sell", "quantity_delta": -1, "quantity_after": 0,
        "price": 100.0, "order_client_id": order_client_id, "reason": "manual", "data_unavailable_fields": "",
    }


def test_load_position_events_returns_empty_frame_when_file_missing():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp_dir:
        missing_path = Path(tmp_dir) / "no_existe.csv"
        df = pe.load_position_events(csv_path=missing_path)
        assert df.empty
        assert list(df.columns) == pe.POSITION_EVENTS_COLUMNS


def test_build_order_client_id_strategy_map_uses_first_valid_row_per_order():
    events_df = pd.DataFrame([
        _event_row("oc-1", "weekly_asymmetric", event_type="ENTRY"),
        _event_row("oc-1", "weekly_asymmetric", event_type="CLOSE"),
        _event_row("oc-2", "scalping"),
        _event_row("oc-3", ""),  # sin strategy_tag -> se ignora
    ])
    mapping = pe.build_order_client_id_strategy_map(events_df)
    assert mapping == {"oc-1": "weekly_asymmetric", "oc-2": "scalping"}
    assert "oc-3" not in mapping


def test_build_order_client_id_strategy_map_empty_when_no_events():
    assert pe.build_order_client_id_strategy_map(pd.DataFrame()) == {}


def test_classify_strategy_from_journal_uses_real_strategy_tag_not_vol_arbitrage_default():
    """
    Regresion DIRECTA del bug real documentado en REPORT.md SS12.0: la
    version vieja (classify_strategy(symbol), todavia disponible sin
    cambios mas abajo en este modulo) etiquetaba CUALQUIER opcion como
    "vol_arbitrage" sin mirar que estrategia la abrio - verificado en
    produccion: de 577 trades exportados como "vol_arbitrage", 411 eran en
    realidad de weekly_asymmetric/scalping. classify_strategy_from_journal
    cruza por client_order_id contra el event journal real en vez de
    adivinar.
    """
    fills = pd.DataFrame([
        _fill_row("2026-09-10T10:00:00Z", "oc-1", "GFGC5200O", "buy", 1, 100.0),
        _fill_row("2026-09-10T10:05:00Z", "oc-2", "GFGV5200O", "buy", 1, 90.0),
        _fill_row("2026-09-10T10:10:00Z", "oc-3", "GFGC6000O", "buy", 1, 80.0),
    ])
    events_df = pd.DataFrame([
        _event_row("oc-1", "weekly_asymmetric", symbol="GFGC5200O"),
        _event_row("oc-2", "scalping", symbol="GFGV5200O"),
        # oc-3 no tiene evento en el journal -> unknown_legacy, NUNCA vol_arbitrage por default
    ])
    result = pe.classify_strategy_from_journal(fills, events_df)
    assert list(result) == ["weekly_asymmetric", "scalping", pe.UNKNOWN_LEGACY_STRATEGY]


def test_classify_strategy_from_journal_still_recognizes_underlying_as_delta_hedge():
    cfg = SETTINGS.instruments
    fills = pd.DataFrame([
        _fill_row("2026-09-10T10:00:00Z", "oc-1", cfg.contado_ticker, "buy", 24, 6975.0),
    ])
    result = pe.classify_strategy_from_journal(fills, pd.DataFrame())
    assert list(result) == ["delta_hedge"]


def test_classify_strategy_from_journal_empty_fills_returns_empty_series():
    result = pe.classify_strategy_from_journal(pd.DataFrame(), pd.DataFrame())
    assert result.empty


def test_build_order_client_id_position_map_uses_first_valid_row_per_order():
    events_df = pd.DataFrame([
        _event_row("oc-1", "weekly_asymmetric", event_type="ENTRY"),
        _event_row("oc-1", "weekly_asymmetric", event_type="CLOSE"),
        _event_row("oc-2", "scalping"),
        _event_row("oc-3", ""),
    ])
    mapping = pe.build_order_client_id_position_map(events_df)
    # _event_row() genera position_id=f"pos-{order_client_id}" - ver helper arriba.
    assert mapping == {"oc-1": "pos-oc-1", "oc-2": "pos-oc-2", "oc-3": "pos-oc-3"}


def test_build_order_client_id_position_map_empty_when_no_events():
    assert pe.build_order_client_id_position_map(pd.DataFrame()) == {}


def test_resolve_position_ids_from_journal_returns_position_id_or_empty_string():
    fills = pd.DataFrame([
        _fill_row("2026-09-10T10:00:00Z", "oc-1", "GFGC5200O", "buy", 1, 100.0),
        _fill_row("2026-09-10T10:05:00Z", "oc-legacy", "GFGV5200O", "buy", 1, 90.0),
    ])
    events_df = pd.DataFrame([_event_row("oc-1", "weekly_asymmetric", symbol="GFGC5200O")])
    result = pe.resolve_position_ids_from_journal(fills, events_df)
    assert list(result) == ["pos-oc-1", ""]  # oc-legacy no tiene evento en el journal -> "" (nunca se fabrica)


def test_match_trades_fifo_reproduces_and_fixes_the_verified_cross_strategy_pnl_crossing():
    """
    Regresion DIRECTA de un bug real, VERIFICADO contra datos de produccion
    (export-lifecycle.csv, 1333 filas, 2026-09-07 a 2026-09-28 - medido en la
    sesion 2026-09-30 a pedido explicito del usuario, ver REPORT.md): la
    posicion `weekly_asymmetric` `9bf25bc4c8ca` sobre el simbolo GFGC7400OC
    (ENTRY +13 @153.001, PARTIAL_EXIT -6 @202.500, quedan 7 contratos
    abiertos que NUNCA se cierran) dejo un lote abierto en la cola FIFO de
    ese simbolo. Cuando `scalping` opero el MISMO simbolo despues (su propio
    round-trip completo: ENTRY +7 @110.99, CLOSE -7 @110.495), el
    match_trades_fifo VIEJO (cola indexada solo por `symbol`) le asigno la
    venta de cierre de scalping contra el lote VIEJO de weekly_asymmetric
    (el mas antiguo en la cola) en vez de contra su propia compra -
    diferencia medida en ese simbolo: ARS 1.361,50 de PnL mal atribuido
    entre ambas estrategias (13,615 unidades * multiplicador 100).

    Esta prueba reproduce el patron EXACTO (mismos precios/cantidades reales
    del export) usando solo la columna "strategy" (sin "position_id" - el
    fallback symbol+estrategia alcanza para este caso, porque las dos
    posiciones son de estrategias DISTINTAS) y confirma que, con el fix,
    scalping cierra contra su PROPIA entrada (pnl=(110.495-110.99)*7=-3.465,
    NO (110.495-153.001)*7=-297.54) y que el lote de weekly_asymmetric
    permanece abierto e intacto.
    """
    fills = pd.DataFrame([
        {**_fill_row("2026-09-15T13:50:23Z", "wa-1", "GFGC7400OC", "buy", 13, 153.001), "strategy": "weekly_asymmetric"},
        {**_fill_row("2026-09-16T13:30:29Z", "wa-2", "GFGC7400OC", "sell", 6, 202.500), "strategy": "weekly_asymmetric"},
        {**_fill_row("2026-09-21T19:34:57Z", "sc-1", "GFGC7400OC", "buy", 7, 110.990), "strategy": "scalping"},
        {**_fill_row("2026-09-21T19:39:14Z", "sc-2", "GFGC7400OC", "sell", 7, 110.495), "strategy": "scalping"},
    ])
    assert "position_id" not in fills.columns  # ejercita el fallback symbol+estrategia, no Position ID

    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=1.0)

    scalping_trades = [t for t in closed if t.strategy == "scalping"]
    weekly_trades = [t for t in closed if t.strategy == "weekly_asymmetric"]

    assert len(scalping_trades) == 1
    assert abs(scalping_trades[0].pnl_ars - (110.495 - 110.990) * 7) < 1e-9
    assert scalping_trades[0].entry_price == 110.990  # contra su PROPIA entrada, NO 153.001

    # El PARTIAL_EXIT de weekly_asymmetric cierra 6 de sus PROPIOS 13 contratos
    # (contra su propia entrada, 153.001->202.5) - eso es correcto y esperado.
    # Lo que NO debe pasar (el bug viejo) es que el cierre de SCALPING toque
    # este lote: los 7 contratos restantes de weekly_asymmetric deben seguir
    # abiertos e intactos, nunca consumidos por la venta de scalping.
    assert len(weekly_trades) == 1
    assert weekly_trades[0].entry_price == 153.001
    assert weekly_trades[0].exit_price == 202.500
    assert weekly_trades[0].quantity == 6.0
    assert len(open_lots) == 1
    assert open_lots[0].strategy == "weekly_asymmetric"
    assert open_lots[0].quantity == 7.0
    assert open_lots[0].entry_price == 153.001


def test_match_trades_fifo_uses_position_id_to_isolate_positions_of_the_same_strategy():
    """
    Complementa el test anterior: cubre el caso que el fallback
    symbol+estrategia NO puede resolver por si solo - dos POSICIONES
    DISTINTAS de la MISMA estrategia sobre el mismo simbolo (el patron de
    fragmentacion ya documentado en AUDITORIA_FASE5.2_LIFECYCLE_ROOT_CAUSE.md:
    "5 bases con mas de 1 Position activa simultanea"). Sin Position ID,
    symbol+estrategia las mezclaria en una sola cola igual que antes.

    posA (weekly_asymmetric) abre y NUNCA cierra (10 @ 100.0). Despues,
    posB (weekly_asymmetric, PosicionID DISTINTO) hace su propio round-trip
    completo (compra 5 @ 50.0, vende 5 @ 60.0). Con Position ID resuelto por
    fill, el cierre de posB debe aparearse contra su PROPIA entrada
    (pnl=(60-50)*5=50), dejando el lote de posA (10 @ 100.0) intacto.
    """
    fills = pd.DataFrame([
        {**_fill_row("2026-09-10T10:00:00Z", "a-1", "GFGC5200O", "buy", 10, 100.0),
         "strategy": "weekly_asymmetric", "position_id": "posA"},
        {**_fill_row("2026-09-10T11:00:00Z", "b-1", "GFGC5200O", "buy", 5, 50.0),
         "strategy": "weekly_asymmetric", "position_id": "posB"},
        {**_fill_row("2026-09-10T12:00:00Z", "b-2", "GFGC5200O", "sell", 5, 60.0),
         "strategy": "weekly_asymmetric", "position_id": "posB"},
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=1.0)

    assert len(closed) == 1
    assert abs(closed[0].pnl_ars - (60.0 - 50.0) * 5) < 1e-9
    assert closed[0].entry_price == 50.0  # contra la entrada de posB, NO la de posA

    assert len(open_lots) == 1
    assert open_lots[0].quantity == 10.0
    assert open_lots[0].entry_price == 100.0  # el lote de posA sigue intacto


def test_load_fills_returns_empty_frame_when_file_missing(tmp_path=None):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp_dir:
        missing_path = Path(tmp_dir) / "no_existe.csv"
        df = pe.load_fills(csv_path=missing_path)
        assert df.empty


def test_load_fills_parses_mixed_subsecond_precision_timestamps_without_dropping_rows():
    """
    Regresion (2026-09-08, BUG REAL descubierto al construir
    ggal_bot/ops/manual_close.py - ver el comentario largo en
    pnl_engine.load_fills()): sin format="ISO8601", pandas infiere el
    formato de fecha del PRIMER valor no nulo de la columna y lo aplica de
    forma ESTRICTA al resto de la columna - dos timestamps ISO 8601
    perfectamente validos pero con distinta precision de sub-segundo (uno
    sin microsegundos, otro con) hacian que el segundo se parseara como
    NaT y lo descartara dropna() EN SILENCIO, sin ningun warning. Esto
    afecta tanto al dashboard como a
    ggal_bot.portfolio.reconciliation.reconstruct_positions_from_shadow_log
    (misma funcion), que run_bot.py corre en CADA arranque del bot en modo
    shadow - un fill perdido asi habria hecho reconstruir una posicion con
    cantidad neta incorrecta sin ningun error visible.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp_dir:
        csv_path = Path(tmp_dir) / "shadow_trades.csv"
        csv_path.write_text(
            "timestamp_utc,client_order_id,symbol,side,order_type,quantity,"
            "requested_price,fill_price,reference_price,event\n"
            "2026-09-01T10:00:00+00:00,cid1,GFGC7000OC,buy,limit,9.0,480.0,480.0,480.0,shadow_fill\n"
            "2026-09-08T16:34:52.077279+00:00,cid2,GFGC7000OC,sell,market,9.0,500.0,500.0,500.0,shadow_fill\n"
        )
        fills = pe.load_fills(csv_path=csv_path)
        assert len(fills) == 2, (
            f"se esperaban 2 fills, se obtuvieron {len(fills)} - el fill con microsegundos "
            "se esta descartando en silencio (ver docstring de este test)"
        )
        assert not fills["timestamp_utc"].isna().any()
        closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
        assert len(open_lots) == 0  # las 9 compradas y las 9 vendidas deben aparearse
        assert len(closed) == 1
        assert closed[0].quantity == 9.0


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


def test_match_trades_fifo_pnl_scales_with_real_quantity_not_a_fixed_value():
    """
    Regresion especifica (TANDA 2 "OPTIMIZACION EJECUTABLE", seccion 6,
    2026-09-08) contra el bug historico de cantidad fija (ver docstring de
    test_match_trades_fifo_uses_multiplier_1_for_delta_hedge_legs mas
    arriba, "unos pocos millones de PnL realizado" fabricados por no usar
    la cantidad real del fill). Usa deliberadamente una cantidad GRANDE y
    poco comun (17 contratos, ni 1 ni 2 - los valores ya cubiertos por los
    tests de arriba) para que un regreso a "cantidad fija = 1" (o
    cualquier otro valor fijo) haga fallar este test de inmediato: el PnL
    esperado es EXACTAMENTE (precio_venta - precio_compra) * 17 *
    multiplicador, ni mas ni menos.
    """
    fills = pd.DataFrame([
        _fill_row("2026-01-01T10:00:00Z", "q1", "GFGC5200O", "buy", 17, 80.0),
        _fill_row("2026-01-01T10:05:00Z", "q2", "GFGC5200O", "sell", 17, 95.0),
    ])
    closed, open_lots = pe.match_trades_fifo(fills, option_multiplier=100.0)
    assert len(open_lots) == 0
    assert len(closed) == 1
    trade = closed[0]
    assert trade.quantity == 17
    expected_pnl = (95.0 - 80.0) * 17 * 100.0  # = 25.500,0 - NO 1500.0 (lo que daria qty fija=1)
    assert trade.pnl_ars == expected_pnl
    assert trade.pnl_ars != (95.0 - 80.0) * 1 * 100.0


ALL_TESTS = [
    test_classify_strategy_uses_contado_and_futuro_tickers,
    test_multiplier_for_symbol_is_1_for_underlying_and_option_multiplier_for_options,
    test_classify_and_multiplier_recognize_bare_underlying_symbol_alias,
    test_match_trades_fifo_uses_multiplier_1_for_delta_hedge_legs,
    test_match_trades_fifo_uses_the_strategy_column_when_present,
    test_match_trades_fifo_falls_back_to_classify_strategy_when_column_absent,
    test_aggregate_open_positions_also_uses_the_strategy_column_when_present,
    test_mark_to_market_uses_multiplier_1_for_delta_hedge_open_position,
    test_summary_pnl_total_not_inflated_when_delta_hedge_and_options_mixed,
    test_match_trades_fifo_closes_simple_round_trip,
    test_match_trades_fifo_handles_short_round_trip,
    test_match_trades_fifo_partial_close_leaves_open_remainder,
    test_match_trades_fifo_reproduces_the_reported_reentry_bug_pattern_correctly,
    test_match_trades_fifo_pnl_scales_with_real_quantity_not_a_fixed_value,
    test_aggregate_open_positions_weighted_average_price,
    test_mark_to_market_computes_unrealized_pnl_from_bot_state,
    test_mark_to_market_flags_missing_price_as_zero_pnl,
    test_compute_summary_win_rate_and_profit_factor,
    test_compute_max_drawdown_on_synthetic_equity_curve,
    test_load_fills_returns_empty_frame_when_file_missing,
    test_load_fills_parses_mixed_subsecond_precision_timestamps_without_dropping_rows,
    test_fit_smile_curve_returns_quadratic_shape,
    test_load_position_events_returns_empty_frame_when_file_missing,
    test_build_order_client_id_strategy_map_uses_first_valid_row_per_order,
    test_build_order_client_id_strategy_map_empty_when_no_events,
    test_classify_strategy_from_journal_uses_real_strategy_tag_not_vol_arbitrage_default,
    test_classify_strategy_from_journal_still_recognizes_underlying_as_delta_hedge,
    test_classify_strategy_from_journal_empty_fills_returns_empty_series,
    test_build_order_client_id_position_map_uses_first_valid_row_per_order,
    test_build_order_client_id_position_map_empty_when_no_events,
    test_resolve_position_ids_from_journal_returns_position_id_or_empty_string,
    test_match_trades_fifo_reproduces_and_fixes_the_verified_cross_strategy_pnl_crossing,
    test_match_trades_fifo_uses_position_id_to_isolate_positions_of_the_same_strategy,
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
