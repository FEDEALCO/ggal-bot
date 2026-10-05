"""
test_shadow_reset.py
======================
Tests de GgalOptionsBot._perform_shadow_reset (Tarea #27 item 5, a pedido
explicito del usuario, sesion 2026-10-01): "al arrancar despues del deploy
cerra todas las posiciones shadow abiertas al mid vigente con un evento
SHADOW_RESET en el journal (con la lista de lo cerrado), y arranca desde
cero... disparado con un flag (GGAL_BOT_SHADOW_RESET_ON_START=true) por un
UNICO arranque, no automatico."

Correr con:
    python -m pytest ggal_bot/validation/test_shadow_reset.py
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timezone

import pytest

pytest.importorskip("pandas")

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionQuote, OrderBookSnapshot
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Position
from ggal_bot.portfolio.reconciliation import reconstruct_positions_from_event_journal
from run_bot import GgalOptionsBot


def _make_quote(symbol: str, strike: float, bid: float, ask: float) -> OptionQuote:
    book = OrderBookSnapshot(symbol, bid=bid, ask=ask, bid_size=50, ask_size=50)
    return OptionQuote(
        symbol=symbol, strike=strike, expiry=date(2026, 10, 16), option_type=OptionType.CALL,
        book=book, days_calendar=15, days_business=11,
    )


def _isolate_position_event_journal(bot):
    import tempfile
    from pathlib import Path
    from ggal_bot.portfolio.event_journal import PositionEventJournal
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    dedicated_path = Path(name)
    dedicated_path.unlink()
    bot.position_event_journal = PositionEventJournal(path=dedicated_path)
    return dedicated_path


@pytest.fixture(autouse=True)
def isolated_shadow_trades_path():
    """
    MEJORA 2026-10-05 (necesaria por el fix "que el reset tambien lea
    shadow_trades.csv ademas del journal"): hasta este fix,
    _shadow_audit_isolation.py apuntaba paths.SHADOW_TRADES_LOG a UN SOLO
    archivo compartido para TODA la corrida de pytest, pero era seguro
    porque nada LEIA ese archivo de vuelta durante un test (solo
    ShadowAuditLogger.log_fill() lo escribia, append-only, "sin un estado
    pegajoso que cambie comportamiento entre tests" - ver el docstring de
    ese modulo). Con _perform_shadow_reset() ahora barriendo TODO el
    historial de shadow_trades.csv (reconstruct_positions_from_shadow_log),
    ese ya no es el caso: un test de este archivo que no aisle su propio
    shadow_trades.csv heredaria los fills de CUALQUIER test anterior en la
    misma corrida (confirmado de forma real: sin este fixture, GFGC7000OC/
    GFGV5000OC de otros tests de este mismo archivo aparecian como "patas
    invisibles" en test_shadow_reset_always_writes_a_checkpoint_event_
    even_with_nothing_to_close). Mismo criterio exacto que
    _reset_shared_kill_switch_state en conftest.py para el mismo problema
    con KillSwitch - aca solo en este archivo (es el unico que ejercita
    _perform_shadow_reset) en vez de global, para no alterar el
    comportamiento de aislamiento ya probado del resto de la suite.
    """
    import tempfile
    from pathlib import Path
    from ggal_bot import paths as _paths

    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    fresh_path = Path(name)
    fresh_path.unlink()
    original = _paths.SHADOW_TRADES_LOG
    _paths.SHADOW_TRADES_LOG = fresh_path
    yield fresh_path
    _paths.SHADOW_TRADES_LOG = original


def test_shadow_reset_closes_open_positions_at_mid_and_zeroes_them(tmp_path):
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        journal_path = _isolate_position_event_journal(bot)
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", 7000.0, bid=100.0, ask=104.0))

        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, entry_price=90.0,
            entry_time=datetime(2026, 9, 28, tzinfo=timezone.utc), strategy_tag="weekly_asymmetric",
        ))

        bot._perform_shadow_reset()

        assert bot._position_quantity("GFGC7000OC") == 0.0

        import csv
        rows = list(csv.DictReader(journal_path.read_text(encoding="utf-8").splitlines()))
        reset_rows = [r for r in rows if r["event_type"] == "SHADOW_RESET"]
        # FIX 2026-10-05 (a pedido explicito del usuario): ahora hay SIEMPRE
        # una fila "checkpoint" ademas de la fila por-posicion de abajo (ver
        # test_shadow_reset_always_writes_a_checkpoint_event_even_with_
        # nothing_to_close para la regresion dedicada de esa fila nueva).
        assert len(reset_rows) == 2
        position_row = next(r for r in reset_rows if r["symbol"] == "GFGC7000OC")
        assert position_row["side"] == "sell"
        assert float(position_row["quantity_delta"]) == -5.0
        assert float(position_row["quantity_after"]) == 0.0
        assert float(position_row["price"]) == pytest.approx(102.0)  # mid de 100/104
        assert position_row["reason"] == "shadow_reset"

        checkpoint_row = next(r for r in reset_rows if r["symbol"] == "")
        assert checkpoint_row["reason"].startswith("shadow_reset_checkpoint:")
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_shadow_reset_writes_a_closing_fill_to_shadow_trades_csv(tmp_path):
    from dashboard.pnl_engine import load_fills

    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        _isolate_position_event_journal(bot)
        shadow_path = tmp_path / "shadow_trades.csv"
        from ggal_bot.execution.order_gateway import ShadowAuditLogger
        bot.order_gateway._shadow_logger = ShadowAuditLogger(path=shadow_path)
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", 7000.0, bid=100.0, ask=104.0))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, entry_price=90.0,
            entry_time=datetime(2026, 9, 28, tzinfo=timezone.utc), strategy_tag="weekly_asymmetric",
        ))

        bot._perform_shadow_reset()

        fills = load_fills(shadow_path)
        assert len(fills) == 1
        row = fills.iloc[0]
        assert row["side"] == "sell"
        assert row["quantity"] == 5.0
        assert row["fill_price"] == pytest.approx(102.0)
        assert str(row["client_order_id"]).startswith("shadow-reset-")
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_shadow_reset_skips_position_without_tradeable_quote_and_keeps_it_open():
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        _isolate_position_event_journal(bot)
        # Sin upsert_quote: GFGC9999OC no esta en el option_chain en absoluto.
        bot.portfolio.add(Position(
            symbol="GFGC9999OC", quantity=3.0, multiplier=100.0, entry_price=50.0,
            entry_time=datetime(2026, 9, 28, tzinfo=timezone.utc), strategy_tag="weekly_asymmetric",
        ))

        bot._perform_shadow_reset()

        assert bot._position_quantity("GFGC9999OC") == 3.0, "sin cotizacion operable, nunca se fabrica un precio de cierre"
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_shadow_reset_handles_short_wing_position_with_buy_side():
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        journal_path = _isolate_position_event_journal(bot)
        bot.option_chain.upsert_quote(_make_quote("GFGV5000OC", 5000.0, bid=5.0, ask=7.0))
        bot.portfolio.add(Position(
            symbol="GFGV5000OC", quantity=-75.0, multiplier=100.0, entry_price=6.1205,
            entry_time=datetime(2026, 10, 1, tzinfo=timezone.utc), strategy_tag="weekly_asymmetric",
            financed_by_symbol="GFGV5400OC",
        ))

        bot._perform_shadow_reset()

        assert bot._position_quantity("GFGV5000OC") == 0.0
        import csv
        rows = list(csv.DictReader(journal_path.read_text(encoding="utf-8").splitlines()))
        reset_rows = [r for r in rows if r["event_type"] == "SHADOW_RESET"]
        position_row = next(r for r in reset_rows if r["symbol"] == "GFGV5000OC")
        assert position_row["side"] == "buy"
        assert float(position_row["quantity_delta"]) == 75.0
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_reconstruct_positions_from_event_journal_treats_shadow_reset_as_a_close(tmp_path):
    """
    Regresion de integracion: una vez que SHADOW_RESET quedo en el
    journal, un restart posterior (reconciliacion) debe ver la posicion
    CERRADA, no volver a abrirla.
    """
    import csv
    path = tmp_path / "position_events.csv"
    header = [
        "timestamp_utc", "event_type", "position_id", "contract_key",
        "symbol", "strategy_tag", "side", "quantity_delta", "quantity_after",
        "price", "order_client_id", "reason", "data_unavailable_fields",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerow(["2026-09-28T14:05:53+00:00", "ENTRY", "8073aff38d40", "", "GFGC6600OC",
                    "weekly_asymmetric", "buy", 16.0, 16.0, 119.0, "", "", ""])
        w.writerow(["2026-10-02T13:00:00+00:00", "SHADOW_RESET", "8073aff38d40", "", "GFGC6600OC",
                    "weekly_asymmetric", "sell", -16.0, 0.0, 95.0, "shadow-reset-abc", "shadow_reset", ""])

    positions, warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)
    assert positions == []
    assert not any("no se pudo asociar" in w for w in warnings)


def test_shadow_reset_pending_flag_set_from_config():
    original_enabled = SETTINGS.shadow.enabled
    original_reset = SETTINGS.shadow.reset_on_start
    SETTINGS.shadow.enabled = True
    SETTINGS.shadow.reset_on_start = True
    try:
        bot = GgalOptionsBot()
        assert bot._shadow_reset_pending is True
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.shadow.reset_on_start = original_reset


def test_shadow_reset_pending_false_by_default():
    original_enabled = SETTINGS.shadow.enabled
    original_reset = SETTINGS.shadow.reset_on_start
    SETTINGS.shadow.enabled = True
    SETTINGS.shadow.reset_on_start = False
    try:
        bot = GgalOptionsBot()
        assert bot._shadow_reset_pending is False
    finally:
        SETTINGS.shadow.enabled = original_enabled
        SETTINGS.shadow.reset_on_start = original_reset


def test_shadow_reset_always_writes_a_checkpoint_event_even_with_nothing_to_close():
    """
    BUG REAL CORREGIDO (a pedido explicito del usuario, 2026-10-05):
    verificado contra produccion que, con el portfolio vacio (el caso mas
    comun: el bot ya arranca sin ninguna posicion abierta segun el Event
    Journal), _perform_shadow_reset() no escribia NINGUNA fila en el
    journal - solo un logger.info(). Con GGAL_BOT_SHADOW_RESET_ON_START=true
    activo desde el deploy y corriendo en cada restart, esto dejo CERO
    eventos SHADOW_RESET reales en logs/position_events.csv a pesar de
    multiples restarts confirmados - exactamente lo que impedia que
    dashboard/data/shadow_reset.py (most_recent_shadow_reset_timestamp)
    pudiera trazar nunca el corte de PnL "antes/despues" que el usuario
    pidio. Ahora: SIEMPRE se escribe un evento checkpoint (symbol/
    position_id vacios, quantity_delta=quantity_after=0.0), aunque no haya
    nada que cerrar.
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        journal_path = _isolate_position_event_journal(bot)
        assert bot.portfolio.positions == []

        bot._perform_shadow_reset()

        import csv
        rows = list(csv.DictReader(journal_path.read_text(encoding="utf-8").splitlines()))
        reset_rows = [r for r in rows if r["event_type"] == "SHADOW_RESET"]
        assert len(reset_rows) == 1, "debe quedar exactamente un evento checkpoint, aunque no se cerro nada"
        row = reset_rows[0]
        assert row["symbol"] == ""
        assert row["position_id"] == ""
        assert float(row["quantity_delta"]) == 0.0
        assert float(row["quantity_after"]) == 0.0
        assert row["reason"] == "shadow_reset_checkpoint: 0 posicion(es) cerradas, 0 omitida(s) por falta de cotizacion operable"

        # most_recent_shadow_reset_timestamp (dashboard/data/shadow_reset.py)
        # debe poder encontrar este checkpoint - es, de hecho, el motivo real
        # de este fix.
        from dashboard.pnl_engine import load_position_events
        from dashboard.data.shadow_reset import most_recent_shadow_reset_timestamp
        events_df = load_position_events(journal_path)
        assert most_recent_shadow_reset_timestamp(events_df) is not None
    finally:
        SETTINGS.shadow.enabled = original_enabled


def _write_raw_shadow_fill(path, *, symbol: str, side: str, quantity: float, fill_price: float, timestamp_utc: str, client_order_id: str):
    """
    Escribe UN fill crudo directo con ShadowAuditLogger - simula un fill
    "de la era mock" que nunca tuvo correspondencia en el Event Journal
    (exactamente el patron de la pata real de -335,85 acciones del
    2026-09-01: logging de delta_hedge al journal recien se agrego el
    2026-09-30). No pasa por GgalOptionsBot ni por ninguna logica de
    estrategia - es, deliberadamente, SOLO lo que shadow_trades.csv ve.
    """
    from ggal_bot.execution.order_gateway import ShadowAuditLogger, OrderRequest, OrderSide, OrderTypeEnum
    logger_ = ShadowAuditLogger(path=path)
    request = OrderRequest(
        symbol=symbol, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        quantity=quantity, price=fill_price, order_type=OrderTypeEnum.MARKET,
        client_order_id=client_order_id,
    )
    logger_.log_fill(request, fill_price=fill_price, reference_price=fill_price)
    # Fuerza el timestamp exacto (log_fill usa datetime.now(), no sirve para
    # fijar una fecha historica especifica tipo "era mock") sobrescribiendo
    # la fila recien escrita.
    import csv
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    header, data_rows = rows[0], rows[1:]
    ts_idx = header.index("timestamp_utc")
    data_rows[-1][ts_idx] = timestamp_utc
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(data_rows)


def test_shadow_reset_also_sweeps_shadow_trades_csv_for_legs_invisible_to_the_journal(isolated_shadow_trades_path):
    """
    MEJORA 2026-10-05 (a pedido explicito del usuario: "que el reset
    tambien lea shadow_trades.csv ademas del journal, para que en el
    futuro no queden patas invisibles sin cerrar"). Reproduce el patron
    real encontrado el 2026-10-05: un fill de hedge sobre el subyacente
    que SOLO existe en logs/shadow_trades.csv (nunca llego al Event
    Journal) - self.portfolio.positions queda vacio (como lo estaria tras
    _reconcile_portfolio_on_startup, que SOLO lee el journal), pero el
    reset debe encontrar esta pata igual y cerrarla, dejando constancia
    explicita en el journal de que vino del barrido, no del journal.

    `isolated_shadow_trades_path` (fixture autouse de este archivo): el
    MISMO path ya aislado que usa paths.SHADOW_TRADES_LOG para este test -
    se escribe el fill crudo directo ahi, sin crear un segundo archivo
    separado.
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        journal_path = _isolate_position_event_journal(bot)

        from ggal_bot.execution.order_gateway import ShadowAuditLogger
        shadow_path = isolated_shadow_trades_path

        _write_raw_shadow_fill(
            shadow_path, symbol=SETTINGS.instruments.contado_ticker, side="sell",
            quantity=335.85, fill_price=5900.0, timestamp_utc="2026-09-01T19:47:00+00:00",
            client_order_id="mock-era-hedge-abc123",
        )

        # self.portfolio queda vacio - exactamente como lo dejaria
        # _reconcile_portfolio_on_startup (solo lee el journal, que no tiene
        # ningun rastro de este fill).
        assert bot.portfolio.positions == []
        bot._spot_book = OrderBookSnapshot(
            SETTINGS.instruments.contado_ticker, bid=5898.0, ask=5902.0, bid_size=5000, ask_size=5000,
        )
        bot.order_gateway._shadow_logger = ShadowAuditLogger(path=shadow_path)

        bot._perform_shadow_reset()

        import csv
        rows = list(csv.DictReader(journal_path.read_text(encoding="utf-8").splitlines()))
        reset_rows = [r for r in rows if r["event_type"] == "SHADOW_RESET"]
        leg_row = next((r for r in reset_rows if r["symbol"] == SETTINGS.instruments.contado_ticker), None)
        assert leg_row is not None, "la pata invisible deberia haber sido cerrada por el barrido de shadow_trades.csv"
        assert leg_row["reason"] == "shadow_reset_invisible_leg_from_shadow_trades_csv"
        assert leg_row["side"] == "buy"  # la pata original era SELL (-335.85) -> se cierra comprando
        assert float(leg_row["quantity_delta"]) == pytest.approx(335.85)

        checkpoint_row = next(r for r in reset_rows if r["symbol"] == "")
        assert "1 posicion(es) cerradas" in checkpoint_row["reason"]

        # Y el fill de cierre quedo en shadow_trades.csv, como cualquier otro cierre de reset.
        from dashboard.pnl_engine import load_fills
        fills = load_fills(shadow_path)
        closing_fills = fills[fills["client_order_id"].str.startswith("shadow-reset-")]
        assert len(closing_fills) == 1
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_shadow_reset_does_not_double_close_a_symbol_already_covered_by_the_journal(isolated_shadow_trades_path):
    """
    Companero defensivo del test de arriba: si el Event Journal YA conoce
    el simbolo (self.portfolio tiene una Position real para el), el
    barrido de shadow_trades.csv NO debe volver a cerrarlo por su cuenta -
    sin esto, un simbolo con fills en ambas fuentes se cerraria DOS veces
    (una vez por el loop principal, otra por el barrido), duplicando el
    fill de cierre y el evento de journal.
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        journal_path = _isolate_position_event_journal(bot)

        from ggal_bot.execution.order_gateway import ShadowAuditLogger
        shadow_path = isolated_shadow_trades_path

        # shadow_trades.csv SI tiene fills para GFGC7000OC (ej. de antes del
        # deploy del journal), pero el Event Journal (y por lo tanto
        # self.portfolio, via _reconcile_portfolio_on_startup) tambien lo
        # conoce HOY - no es una pata invisible, es una posicion normal.
        _write_raw_shadow_fill(
            shadow_path, symbol="GFGC7000OC", side="buy", quantity=5.0, fill_price=90.0,
            timestamp_utc="2026-09-20T14:00:00+00:00", client_order_id="old-entry-xyz",
        )

        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC", 7000.0, bid=100.0, ask=104.0))
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, entry_price=90.0,
            entry_time=datetime(2026, 9, 28, tzinfo=timezone.utc), strategy_tag="weekly_asymmetric",
        ))
        bot.order_gateway._shadow_logger = ShadowAuditLogger(path=shadow_path)

        bot._perform_shadow_reset()

        import csv
        rows = list(csv.DictReader(journal_path.read_text(encoding="utf-8").splitlines()))
        reset_rows = [r for r in rows if r["event_type"] == "SHADOW_RESET" and r["symbol"] == "GFGC7000OC"]
        assert len(reset_rows) == 1, "el simbolo ya cubierto por el journal no debe cerrarse una segunda vez via el barrido"
        assert reset_rows[0]["reason"] == "shadow_reset"  # vino del loop principal, no del barrido
    finally:
        SETTINGS.shadow.enabled = original_enabled


def test_shadow_reset_checkpoint_event_never_fabricates_a_phantom_position_on_reconciliation():
    """
    Companero defensivo del fix de arriba: el evento checkpoint
    (symbol="", quantity_delta=0.0) jamas debe interpretarse, en un
    restart posterior, como una posicion real que hay que reconstruir -
    reconstruct_positions_from_event_journal() debe seguir devolviendo []
    para un journal que solo tiene este checkpoint.
    """
    from ggal_bot.portfolio.reconciliation import reconstruct_positions_from_event_journal

    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        journal_path = _isolate_position_event_journal(bot)
        assert bot.portfolio.positions == []

        bot._perform_shadow_reset()

        positions, warnings = reconstruct_positions_from_event_journal(csv_path=journal_path, option_multiplier=100.0)
        assert positions == []
        assert warnings == []
    finally:
        SETTINGS.shadow.enabled = original_enabled
