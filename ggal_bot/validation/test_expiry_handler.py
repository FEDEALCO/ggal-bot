"""
test_expiry_handler.py
========================
Tests de ggal_bot.portfolio.expiry_handler.close_expired_positions -
PRIORIDAD 1, 2026-10-21 (bug de riesgo permitido durante el freeze, ver
FREEZE.md y RiskConfig.enable_expiry_autoclose en config.py para el
hallazgo real completo que motiva este modulo: 8 posiciones de produccion
de la serie de Octubre, vencida el 2026-10-16, seguian "abiertas" 5 dias
despues, aportando delta fantasma al kill switch y bloqueando entradas
nuevas via has_unknown_greeks).

Correr con:
    python -m pytest ggal_bot/validation/test_expiry_handler.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from ggal_bot.portfolio.event_journal import PositionEventJournal
from ggal_bot.portfolio.portfolio import Portfolio, Position
from ggal_bot.portfolio.expiry_handler import (
    close_expired_positions,
    intrinsic_value,
    parse_option_type_and_strike,
)


def _journal(tmp_path: Path) -> PositionEventJournal:
    return PositionEventJournal(path=tmp_path / "position_events.csv")


def _read_rows(path: Path) -> list:
    return list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))


def _pos(symbol, quantity, expiry, strategy_tag="weekly_asymmetric", position_id="pid1", contract_key=None):
    return Position(
        symbol=symbol, quantity=quantity, multiplier=100.0,
        greeks_per_unit={"delta": 0.45, "gamma": 0.01, "vega": 1.2, "theta": -0.5},
        expiry=expiry, strategy_tag=strategy_tag, position_id=position_id,
        contract_key=contract_key or f"GGAL|{symbol}|{expiry.isoformat()}",
    )


# -- parse_option_type_and_strike / intrinsic_value (unidades) --------------

def test_parse_option_type_and_strike_call():
    assert parse_option_type_and_strike("GFGC7000OC") == ("call", 7000.0)


def test_parse_option_type_and_strike_put():
    assert parse_option_type_and_strike("GFGV6400I") == ("put", 6400.0)


def test_parse_option_type_and_strike_unknown_symbol_returns_none():
    assert parse_option_type_and_strike("MERV - XMEV - GGAL - 24hs") is None


def test_intrinsic_value_call_itm_otm():
    assert intrinsic_value("call", strike=6000.0, spot=6500.0) == pytest.approx(500.0)
    assert intrinsic_value("call", strike=6000.0, spot=5500.0) == pytest.approx(0.0)


def test_intrinsic_value_put_itm_otm():
    assert intrinsic_value("put", strike=6000.0, spot=5500.0) == pytest.approx(500.0)
    assert intrinsic_value("put", strike=6000.0, spot=6500.0) == pytest.approx(0.0)


# -- close_expired_positions: los 4 casos pedidos explicitamente por el usuario --
# ("Tests con call/put ITM y OTM, larga y corta")

def test_long_call_otm_closes_at_zero(tmp_path):
    portfolio = Portfolio()
    pos = _pos("GFGC7000OC", quantity=10.0, expiry=date(2026, 10, 16))
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)  # bien despues del vencimiento

    closures = close_expired_positions(portfolio, journal, spot=6500.0, now=now)

    assert len(closures) == 1
    c = closures[0]
    assert c.itm is False
    assert c.intrinsic_value == pytest.approx(0.0)
    assert c.was_long is True
    assert pos.quantity == 0.0  # sacada del calculo de Griegas (contribution() = 0 con quantity=0)

    rows = _read_rows(journal._path)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "EXPIRY"
    assert row["symbol"] == "GFGC7000OC"
    assert row["side"] == "sell"  # cerrar una posicion larga = vender
    assert float(row["quantity_delta"]) == pytest.approx(-10.0)
    assert float(row["quantity_after"]) == pytest.approx(0.0)
    assert float(row["price"]) == pytest.approx(0.0)
    assert row["reason"] == "expiry_otm"


def test_long_call_itm_closes_at_intrinsic_assumed_exercise(tmp_path):
    portfolio = Portfolio()
    pos = _pos("GFGC6000OC", quantity=4.0, expiry=date(2026, 10, 16))
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    spot = 6500.0  # ITM: 6500 - 6000 = 500 de intrinseco

    closures = close_expired_positions(portfolio, journal, spot=spot, now=now)

    assert len(closures) == 1
    c = closures[0]
    assert c.itm is True
    assert c.intrinsic_value == pytest.approx(500.0)

    rows = _read_rows(journal._path)
    row = rows[0]
    assert row["reason"] == "expiry_itm_ejercicio_supuesto"
    assert float(row["price"]) == pytest.approx(500.0)
    assert row["side"] == "sell"


def test_long_put_otm_closes_at_zero(tmp_path):
    portfolio = Portfolio()
    pos = _pos("GFGV7000OC", quantity=6.0, expiry=date(2026, 10, 16))
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    spot = 7500.0  # put strike 7000, spot 7500 -> OTM

    closures = close_expired_positions(portfolio, journal, spot=spot, now=now)

    assert closures[0].itm is False
    assert closures[0].intrinsic_value == pytest.approx(0.0)
    row = _read_rows(journal._path)[0]
    assert row["reason"] == "expiry_otm"
    assert float(row["price"]) == pytest.approx(0.0)


def test_long_put_itm_closes_at_intrinsic(tmp_path):
    portfolio = Portfolio()
    pos = _pos("GFGV7000OC", quantity=6.0, expiry=date(2026, 10, 16))
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    spot = 6200.0  # put strike 7000, spot 6200 -> intrinseco 800

    closures = close_expired_positions(portfolio, journal, spot=spot, now=now)

    assert closures[0].itm is True
    assert closures[0].intrinsic_value == pytest.approx(800.0)
    row = _read_rows(journal._path)[0]
    assert row["reason"] == "expiry_itm_ejercicio_supuesto"
    assert float(row["price"]) == pytest.approx(800.0)


def test_short_call_itm_assigned_against(tmp_path):
    """Pata CORTA (vendida) de un call ITM al vencer: se le asigna en
    contra - se cierra 'comprando' al valor intrinseco (el resultado
    economico real de una asignacion, no un precio de mercado fabricado)."""
    portfolio = Portfolio()
    pos = _pos("GFGC6000OC", quantity=-8.0, expiry=date(2026, 10, 16))  # corta
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    spot = 6800.0  # ITM: intrinseco 800

    closures = close_expired_positions(portfolio, journal, spot=spot, now=now)

    assert len(closures) == 1
    c = closures[0]
    assert c.was_long is False
    assert c.itm is True
    assert c.intrinsic_value == pytest.approx(800.0)
    assert pos.quantity == 0.0

    row = _read_rows(journal._path)[0]
    assert row["side"] == "buy"  # cerrar una posicion corta = comprar (cubrirse)
    assert float(row["quantity_delta"]) == pytest.approx(8.0)  # +8 para pasar de -8 a 0
    assert float(row["quantity_after"]) == pytest.approx(0.0)
    assert row["reason"] == "expiry_itm_ejercicio_supuesto"


def test_short_put_otm_closes_at_zero(tmp_path):
    portfolio = Portfolio()
    pos = _pos("GFGV6000OC", quantity=-12.0, expiry=date(2026, 10, 16))  # corta
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    spot = 6800.0  # put strike 6000, spot 6800 -> OTM

    closures = close_expired_positions(portfolio, journal, spot=spot, now=now)

    assert closures[0].was_long is False
    assert closures[0].itm is False
    row = _read_rows(journal._path)[0]
    assert row["side"] == "buy"
    assert float(row["quantity_delta"]) == pytest.approx(12.0)


# -- Casos de borde ----------------------------------------------------------

def test_not_yet_expired_is_left_untouched(tmp_path):
    portfolio = Portfolio()
    pos = _pos("GFGC7000NO", quantity=3.0, expiry=date(2026, 11, 20))
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)

    closures = close_expired_positions(portfolio, journal, spot=6500.0, now=now)

    assert closures == []
    assert pos.quantity == 3.0
    assert not journal._path.exists() or _read_rows(journal._path) == []


def test_expiry_today_waits_until_session_close(tmp_path):
    """Vence HOY: no se cierra mientras la rueda de hoy todavia esta
    abierta (ver market_hours.is_after_session_close) - 'al cierre del dia
    de vencimiento', no antes."""
    portfolio = Portfolio()
    pos = _pos("GFGC7000OC", quantity=5.0, expiry=date(2026, 10, 16))
    portfolio.add(pos)
    journal = _journal(tmp_path)

    during_session = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)  # ~12:00 ART, rueda abierta
    closures = close_expired_positions(portfolio, journal, spot=6500.0, now=during_session)
    assert closures == []
    assert pos.quantity == 5.0

    after_close = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)  # ~18:00 ART, rueda ya cerro
    closures = close_expired_positions(portfolio, journal, spot=6500.0, now=after_close)
    assert len(closures) == 1
    assert pos.quantity == 0.0


def test_already_closed_position_is_skipped(tmp_path):
    """quantity==0 (ya cerrada por cualquier via) nunca genera un evento EXPIRY duplicado."""
    portfolio = Portfolio()
    pos = _pos("GFGC7000OC", quantity=0.0, expiry=date(2026, 10, 16))
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)

    closures = close_expired_positions(portfolio, journal, spot=6500.0, now=now)
    assert closures == []


def test_idempotent_across_repeated_calls(tmp_path):
    """Llamar la funcion varias veces en el mismo ciclo (o ciclos sucesivos)
    nunca re-cierra una posicion ya cerrada ni duplica el evento EXPIRY -
    mismo criterio de idempotencia que el resto de las guardias de riesgo
    del bot (ver _rotate_if_new_day_locked en data/market_snapshot_log.py)."""
    portfolio = Portfolio()
    pos = _pos("GFGC7000OC", quantity=10.0, expiry=date(2026, 10, 16))
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)

    first = close_expired_positions(portfolio, journal, spot=6500.0, now=now)
    second = close_expired_positions(portfolio, journal, spot=6600.0, now=now)

    assert len(first) == 1
    assert second == []
    assert len(_read_rows(journal._path)) == 1  # una sola fila EXPIRY, no dos


def test_position_without_expiry_is_left_untouched(tmp_path):
    """Nunca se fabrica un vencimiento: sin `expiry` poblado, la posicion no se toca."""
    portfolio = Portfolio()
    pos = Position(symbol="GFGC7000OC", quantity=5.0, multiplier=100.0, expiry=None)
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)

    closures = close_expired_positions(portfolio, journal, spot=6500.0, now=now)
    assert closures == []
    assert pos.quantity == 5.0


def test_unparseable_symbol_is_left_untouched_not_fabricated(tmp_path, caplog):
    """Un symbol que no matchea el patron de opciones GGAL (ej. el
    subyacente, o un ticker con formato desconocido) nunca se cierra con un
    strike/option_type inventado - se deja intacta y se loguea un warning."""
    portfolio = Portfolio()
    pos = _pos("MERV - XMEV - GGAL - 24hs", quantity=100.0, expiry=date(2026, 10, 16))
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)

    closures = close_expired_positions(portfolio, journal, spot=6500.0, now=now)
    assert closures == []
    assert pos.quantity == 100.0


def test_closed_position_removed_from_aggregate_greeks(tmp_path):
    """El caso real que motiva todo esto: tras cerrar, Portfolio.total_greeks()
    ya NO incluye el delta congelado de la posicion vencida."""
    portfolio = Portfolio()
    pos = _pos("GFGC7000OC", quantity=10.0, expiry=date(2026, 10, 16))
    portfolio.add(pos)
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)

    before = portfolio.total_greeks()
    assert before["delta"] != 0.0  # el delta congelado SI estaba aportando antes de cerrar

    close_expired_positions(portfolio, journal, spot=6500.0, now=now)

    after = portfolio.total_greeks()
    assert after == {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}


def test_multiple_expired_positions_in_one_call(tmp_path):
    portfolio = Portfolio()
    portfolio.add(_pos("GFGC7000OC", quantity=10.0, expiry=date(2026, 10, 16), position_id="pid_a"))
    portfolio.add(_pos("GFGV6000OC", quantity=-5.0, expiry=date(2026, 10, 16), position_id="pid_b"))
    portfolio.add(_pos("GFGC7000NO", quantity=3.0, expiry=date(2026, 11, 20), position_id="pid_c"))  # no vencida todavia
    journal = _journal(tmp_path)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)

    closures = close_expired_positions(portfolio, journal, spot=6500.0, now=now)

    assert {c.position_id for c in closures} == {"pid_a", "pid_b"}
    rows = _read_rows(journal._path)
    assert len(rows) == 2
    assert all(r["event_type"] == "EXPIRY" for r in rows)
