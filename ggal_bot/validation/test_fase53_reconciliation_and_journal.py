"""
test_fase53_reconciliation_and_journal.py
===========================================
Tests de regresion (Fase 5.3, COMMIT 2) para:

  1. ggal_bot/portfolio/reconciliation.py::reconstruct_positions_from_shadow_log
     - reconstruye la posicion NETA abierta por simbolo desde un CSV de
     fills estilo logs/shadow_trades.csv, reusando dashboard.pnl_engine.
     match_trades_fifo (el mismo algoritmo ya verificado en Fase 5/5.1).

  2. ggal_bot/portfolio/event_journal.py::PositionEventJournal, y su
     wiring en run_bot.py (_act_on_entry_signal escribe ENTRY,
     _act_on_exit_signal escribe REDUCE/PARTIAL_EXIT/CLOSE por LOTE
     efectivamente tocado - ver el fix de over-close del commit anterior).

  3. La reconciliacion de arranque completa (GgalOptionsBot.
     connect_and_subscribe -> _reconcile_portfolio_on_startup) contra un
     shadow_trades.csv con una posicion neta abierta: confirma que Guarda 2
     ve la posicion restaurada (bloquea una entrada nueva sobre esa base),
     que es exactamente el comportamiento que faltaba y que motiva esta
     fase (ver AUDITORIA_FASE5.2B_FORENSIC_REPLAY.md SS13).
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OrderBookSnapshot, OptionQuote
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.paths import POSITION_EVENTS_LOG
from ggal_bot.portfolio.event_journal import PositionEventJournal
from ggal_bot.portfolio.reconciliation import (
    ReconciliationUnavailable,
    reconstruct_positions_from_shadow_log,
)
from ggal_bot.strategy.weekly_asymmetric import EntrySignal
from run_bot import GgalOptionsBot

_SHADOW_HEADER = [
    "timestamp_utc", "client_order_id", "symbol", "side", "order_type",
    "quantity", "requested_price", "fill_price", "reference_price", "event",
]


def _write_shadow_csv(path: Path, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_SHADOW_HEADER)
        for row in rows:
            w.writerow(row)


def _make_quote(symbol: str, strike: float = 7000.0) -> OptionQuote:
    book = OrderBookSnapshot(symbol, bid=570.0, ask=575.0, bid_size=50, ask_size=50)
    quote = OptionQuote(
        symbol=symbol, strike=strike, expiry=date(2026, 9, 18),
        option_type=OptionType.CALL, book=book, days_calendar=17, days_business=12,
    )
    quote.greeks = {"delta": 0.55, "gamma": 0.0009, "vega": 3.2, "theta": -1.4, "rho": 0.2, "price": 572.5}
    quote.iv = 0.55
    return quote


# ---------------------------------------------------------------------------
# 1. reconstruct_positions_from_shadow_log
# ---------------------------------------------------------------------------

def test_reconstruct_open_lot_survives_partial_close(tmp_path):
    """
    3 BUY de 3 (total 9), 1 SELL de 6 (FIFO cierra los primeros 2 lotes
    completos) -> debe quedar 1 lote abierto de 3, con el entry_price/
    entry_time del ULTIMO BUY (el unico que no se cerro).
    """
    csv_path = tmp_path / "shadow_trades.csv"
    _write_shadow_csv(csv_path, [
        ["2026-09-01T20:14:00+00:00", "c1", "GFGC7000OC", "buy", "market", 3, 572.5, 572.5, 572.5, "shadow_fill"],
        ["2026-09-03T15:20:00+00:00", "c2", "GFGC7000OC", "buy", "market", 3, 573.0, 573.0, 573.0, "shadow_fill"],
        ["2026-09-03T15:22:00+00:00", "c3", "GFGC7000OC", "buy", "market", 3, 574.0, 574.0, 574.0, "shadow_fill"],
        ["2026-09-04T10:00:00+00:00", "c4", "GFGC7000OC", "sell", "market", 6, 580.0, 580.0, 580.0, "shadow_fill"],
    ])

    positions, warnings = reconstruct_positions_from_shadow_log(csv_path=csv_path, option_multiplier=100.0)

    assert len(positions) == 1, f"esperaba 1 lote abierto, hubo {len(positions)}"
    pos = positions[0]
    assert pos.symbol == "GFGC7000OC"
    assert pos.quantity == 3.0
    assert pos.entry_price == 574.0
    assert pos.multiplier == 100.0
    # Sin option_chain, no hay griegas vigentes -> warning explicito, nunca fabricado.
    assert any("greeks_per_unit" in w for w in warnings)
    assert pos.greeks_per_unit is None


def test_reconstruct_with_option_chain_fills_greeks(tmp_path):
    csv_path = tmp_path / "shadow_trades.csv"
    _write_shadow_csv(csv_path, [
        ["2026-09-01T20:14:00+00:00", "c1", "GFGC7000OC", "buy", "market", 3, 572.5, 572.5, 572.5, "shadow_fill"],
    ])

    class _FakeChain:
        def get(self, symbol):
            return _make_quote(symbol) if symbol == "GFGC7000OC" else None

    positions, warnings = reconstruct_positions_from_shadow_log(
        csv_path=csv_path, option_multiplier=100.0, option_chain=_FakeChain(),
    )
    assert len(positions) == 1
    assert positions[0].greeks_per_unit is not None
    assert positions[0].expiry == date(2026, 9, 18)
    assert not any("greeks_per_unit" in w for w in warnings)


def test_reconstruct_fully_closed_symbol_yields_no_position(tmp_path):
    csv_path = tmp_path / "shadow_trades.csv"
    _write_shadow_csv(csv_path, [
        ["2026-09-01T20:14:00+00:00", "c1", "GFGC7000OC", "buy", "market", 3, 572.5, 572.5, 572.5, "shadow_fill"],
        ["2026-09-02T10:00:00+00:00", "c2", "GFGC7000OC", "sell", "market", 3, 580.0, 580.0, 580.0, "shadow_fill"],
    ])
    positions, warnings = reconstruct_positions_from_shadow_log(csv_path=csv_path, option_multiplier=100.0)
    assert positions == []


def test_reconstruct_missing_file_returns_empty_without_error(tmp_path):
    positions, warnings = reconstruct_positions_from_shadow_log(csv_path=tmp_path / "no_existe.csv")
    assert positions == []
    assert warnings == []


def test_reconstruct_underlying_symbol_uses_multiplier_one(tmp_path):
    underlying = SETTINGS.instruments.contado_ticker
    csv_path = tmp_path / "shadow_trades.csv"
    _write_shadow_csv(csv_path, [
        ["2026-09-01T20:14:00+00:00", "c1", underlying, "buy", "market", 100, 6600.0, 6600.0, 6600.0, "shadow_fill"],
    ])
    positions, _warnings = reconstruct_positions_from_shadow_log(csv_path=csv_path, option_multiplier=100.0)
    assert len(positions) == 1
    assert positions[0].multiplier == 1.0


# ---------------------------------------------------------------------------
# 2. PositionEventJournal + wiring en run_bot.py
# ---------------------------------------------------------------------------

def test_event_journal_writes_header_and_rows(tmp_path):
    path = tmp_path / "position_events.csv"
    journal = PositionEventJournal(path=path)
    journal.log_event(
        "ENTRY", position_id="abc123", symbol="GFGC7000OC", strategy_tag="weekly_asymmetric",
        side="buy", quantity_delta=3.0, quantity_after=3.0, price=572.5, reason="test",
    )
    content = path.read_text(encoding="utf-8").splitlines()
    assert content[0].startswith("timestamp_utc,event_type,position_id")
    assert "ENTRY" in content[1]
    assert "abc123" in content[1]


def test_bot_entry_and_full_close_write_matching_events():
    """
    Reproduce ENTRY (3 contratos) seguido de un cierre TOTAL (stop_loss) y
    confirma que el event journal real (wireado en run_bot.py) registra
    exactamente 1 fila ENTRY y 1 fila CLOSE para el mismo position_id.
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC"))

        entry_signal = EntrySignal(
            symbol="GFGC7000OC", option_type=OptionType.CALL, reason="test_journal",
            premium_reference=572.5, iv_dislocation_vol_points=5.0, convexity_score=0.01,
        )
        bot._act_on_entry_signal(entry_signal, spot=7050.0)
        assert bot._position_quantity("GFGC7000OC") > 0

        # La entrada deja un contexto de orden "en vigilancia" en
        # mid_price_exec (ver mid_price_exec.py::submit, que lo agrega
        # incondicionalmente) hasta el proximo monitor_and_reprice() del
        # ciclo real (ver recompute_cycle) - sin esto, Guarda 1 de
        # _act_on_exit_signal pospondria la salida ("ya hay una orden en
        # vigilancia"), algo que en el bot real se resuelve solo en el
        # siguiente recompute_cycle().
        quote = bot.option_chain.get("GFGC7000OC")
        bot.mid_price_exec.monitor_and_reprice(current_books={"GFGC7000OC": quote.book}, current_spot=7050.0)

        pos = next(p for p in bot.portfolio.positions if p.symbol == "GFGC7000OC")
        position_id = pos.position_id

        class _ExitSig:
            symbol = "GFGC7000OC"
            reason = "stop_loss"
            action = "sell_to_close"
            quantity = pos.quantity

        bot._act_on_exit_signal(_ExitSig(), spot=7040.0)
        assert bot._position_quantity("GFGC7000OC") == 0.0

        rows = list(csv.DictReader(POSITION_EVENTS_LOG.read_text(encoding="utf-8").splitlines()))
        own_rows = [r for r in rows if r["position_id"] == position_id]
        event_types = [r["event_type"] for r in own_rows]
        assert event_types == ["ENTRY", "CLOSE"], f"secuencia de eventos inesperada: {event_types}"
    finally:
        SETTINGS.shadow.enabled = original_enabled


# ---------------------------------------------------------------------------
# 3. Reconciliacion de arranque end-to-end (Guarda 2 ve la posicion restaurada)
# ---------------------------------------------------------------------------

def test_startup_reconciliation_makes_guard2_block_new_entry(tmp_path, monkeypatch):
    """
    Con una posicion neta abierta en el CSV, tras reconciliar, Guarda 2
    debe bloquear una entrada nueva sobre esa misma base - EXACTAMENTE lo
    que faltaba en produccion (ver ROOT CAUSE de Fase 5.3).
    """
    csv_path = tmp_path / "shadow_trades.csv"
    _write_shadow_csv(csv_path, [
        ["2026-09-01T20:14:00+00:00", "c1", "GFGC7000OC", "buy", "market", 3, 572.5, 572.5, 572.5, "shadow_fill"],
    ])

    import ggal_bot.paths as ggal_paths
    monkeypatch.setattr(ggal_paths, "SHADOW_TRADES_LOG", csv_path)

    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        assert bot._position_quantity("GFGC7000OC") == 0.0, "portfolio arranca vacio antes de reconciliar"

        bot._reconcile_portfolio_on_startup()
        assert bot._position_quantity("GFGC7000OC") == 3.0, "la reconciliacion debia restaurar 3 contratos"

        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC"))
        entry_signal = EntrySignal(
            symbol="GFGC7000OC", option_type=OptionType.CALL, reason="test_reconcile",
            premium_reference=572.5, iv_dislocation_vol_points=5.0, convexity_score=0.01,
        )
        bot._act_on_entry_signal(entry_signal, spot=7050.0)

        assert bot._position_quantity("GFGC7000OC") == 3.0, (
            "BUG: Guarda 2 no bloqueo una entrada nueva pese a que la reconciliacion "
            "restauro una posicion abierta sobre la misma base."
        )
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_reconciliation_disabled_flag_skips_restoration(tmp_path, monkeypatch):
    csv_path = tmp_path / "shadow_trades.csv"
    _write_shadow_csv(csv_path, [
        ["2026-09-01T20:14:00+00:00", "c1", "GFGC7000OC", "buy", "market", 3, 572.5, 572.5, 572.5, "shadow_fill"],
    ])
    import ggal_bot.paths as ggal_paths
    monkeypatch.setattr(ggal_paths, "SHADOW_TRADES_LOG", csv_path)

    original_enabled = SETTINGS.shadow.enabled
    original_flag = SETTINGS.shadow.reconcile_portfolio_on_startup
    SETTINGS.shadow.enabled = True
    SETTINGS.shadow.reconcile_portfolio_on_startup = False
    try:
        bot = GgalOptionsBot()
        bot._reconcile_portfolio_on_startup()
        assert bot._position_quantity("GFGC7000OC") == 0.0, "el flag debia dejar el portfolio vacio"
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.shadow.reconcile_portfolio_on_startup = original_flag
