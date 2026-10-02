"""
test_quantify_historical_mock_fills.py
========================================
Tests para ggal_bot/ops/quantify_historical_mock_fills.py (Tarea #27/#28
item 3(c), a pedido explicito del usuario: "Cuantifica cuantos fills
historicos se hicieron con mock, incluidos los de horario de rueda").

Los timestamps de log usados aca reproducen EXACTAMENTE el formato real
configurado en run_bot.py (logging.Formatter("%(asctime)s [%(levelname)s]
%(name)s: %(message)s"), asctime en hora ART) y los mensajes reales de
ggal_bot/data/live_shadow_feed.py (verificados contra ese modulo, no
inventados) - ver docstring del script para el detalle completo.

Correr con:
    python -m pytest ggal_bot/validation/test_quantify_historical_mock_fills.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

pytest.importorskip("pandas")

from ggal_bot.ops import quantify_historical_mock_fills as qmf

_SHADOW_TRADES_HEADER = (
    "timestamp_utc,client_order_id,symbol,side,order_type,quantity,requested_price,"
    "fill_price,reference_price,event,bid_at_fill,ask_at_fill,mid_at_fill"
)


def _write_log(path: Path, lines) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fill_row(ts_utc_iso, client_order_id, symbol="GFGC7000OC"):
    return (
        f"{ts_utc_iso},{client_order_id},{symbol},buy,limit,1,100.0,100.0,100.0,shadow_fill,99.0,101.0,100.0"
    )


def test_parse_art_timestamp_to_utc_adds_three_hours():
    # ART = UTC-3 fijo -> UTC = ART + 3h (verificado contra ggal_bot/market_hours.py::ART_OFFSET_HOURS = -3.0).
    utc = qmf._parse_art_timestamp_to_utc("2026-09-30 10:00:00,000")
    assert utc == datetime(2026, 9, 30, 13, 0, 0, tzinfo=timezone.utc)


def test_parse_source_transitions_basic_sequence(tmp_path):
    log_path = tmp_path / "ggal_bot.log"
    _write_log(log_path, [
        "2026-09-30 10:00:00,000 [INFO] ggal_bot.data.live_shadow_feed: Shadow feed: fuente activa = 'Data912RestSource' (prioridad 1/2).",
        "2026-09-30 10:05:00,000 [INFO] ggal_bot.run_bot: linea de otro logger, debe ignorarse.",
        "2026-09-30 11:30:00,000 [WARNING] ggal_bot.data.live_shadow_feed: Shadow feed: failover (fallaron 3 polls consecutivos) -> fuente 'MockReplaySource' (prioridad 2/2).",
        "2026-09-30 12:00:00,000 [INFO] ggal_bot.data.live_shadow_feed: Shadow feed: 'Data912RestSource' (mayor prioridad que la fuente activa) volvio a estar disponible; se vuelve a esa fuente.",
        "2026-09-30 12:10:00,000 [ERROR] ggal_bot.data.live_shadow_feed: Shadow feed: fuente 'mock' no se pudo instanciar (RuntimeError simulado).",
    ])
    transitions, misconfig_warnings = qmf.parse_source_transitions([log_path])

    assert [t.source_name for t in transitions] == ["Data912RestSource", "MockReplaySource", "Data912RestSource"]
    assert transitions[0].timestamp_utc == datetime(2026, 9, 30, 13, 0, 0, tzinfo=timezone.utc)
    assert transitions[1].timestamp_utc == datetime(2026, 9, 30, 14, 30, 0, tzinfo=timezone.utc)
    assert transitions[2].timestamp_utc == datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)
    assert len(misconfig_warnings) == 1


def test_parse_source_transitions_exhausted_fallback_succeeds_silently(tmp_path):
    log_path = tmp_path / "ggal_bot.log"
    _write_log(log_path, [
        "2026-09-30 10:00:00,000 [WARNING] ggal_bot.data.live_shadow_feed: Shadow feed: se agotaron todas las fuentes configuradas en source_priority ('primary_ws', 'data912'); se intenta failover final a Mock/Replay.",
        "2026-09-30 10:00:00,010 [INFO] ggal_bot.data.live_shadow_feed: Shadow feed: 1 instrumentos bajo seguimiento (fuente=MockReplaySource).",
    ])
    transitions, _ = qmf.parse_source_transitions([log_path])
    assert len(transitions) == 1
    assert transitions[0].source_name == "MockReplaySource"


def test_parse_source_transitions_exhausted_fallback_disabled_yields_no_data_source(tmp_path):
    log_path = tmp_path / "ggal_bot.log"
    _write_log(log_path, [
        "2026-09-30 10:00:00,000 [WARNING] ggal_bot.data.live_shadow_feed: Shadow feed: se agotaron todas las fuentes configuradas en source_priority ('primary_ws',); se intenta failover final a Mock/Replay.",
        "2026-09-30 10:00:00,010 [ERROR] ggal_bot.data.live_shadow_feed: Shadow feed: ninguna fuente respondio y el fallback a Mock/Replay esta deshabilitado (RuntimeError simulado) - el bot se queda SIN NINGUNA cotizacion.",
    ])
    transitions, _ = qmf.parse_source_transitions([log_path])
    assert len(transitions) == 1
    assert transitions[0].source_name == "_NoDataSource"


def test_parse_source_transitions_exhausted_as_last_line_assumes_mock_succeeded(tmp_path):
    """Sin ninguna linea posterior del logger que confirme o desmienta - se asume el
    comportamiento historico real (fallback incondicional, sin ShadowConfig.allow_mock_source)."""
    log_path = tmp_path / "ggal_bot.log"
    _write_log(log_path, [
        "2026-09-30 10:00:00,000 [WARNING] ggal_bot.data.live_shadow_feed: Shadow feed: se agotaron todas las fuentes configuradas en source_priority ('primary_ws',); se intenta failover final a Mock/Replay.",
    ])
    transitions, _ = qmf.parse_source_transitions([log_path])
    assert len(transitions) == 1
    assert transitions[0].source_name == "MockReplaySource"


def test_active_source_at_returns_unknown_before_first_transition():
    transitions = [qmf.SourceTransition(datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc), "Data912RestSource", "")]
    before = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert qmf.active_source_at(transitions, before) == qmf._UNKNOWN_SOURCE


def test_active_source_at_returns_last_transition_at_or_before_timestamp():
    transitions = [
        qmf.SourceTransition(datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc), "Data912RestSource", ""),
        qmf.SourceTransition(datetime(2026, 9, 30, 14, 30, tzinfo=timezone.utc), "MockReplaySource", ""),
    ]
    mid = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
    after = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
    assert qmf.active_source_at(transitions, mid) == "Data912RestSource"
    assert qmf.active_source_at(transitions, after) == "MockReplaySource"


def test_quantify_end_to_end_classifies_fills_by_source_and_session(tmp_path):
    """
    Escenario: la fuente arranca en Data912RestSource y conmuta a Mock a
    las 14:30 UTC (11:30 ART) del martes 2026-09-29. Un fill a las 14:00 UTC
    (dentro del horario de rueda asumido, 11:00-17:00 ART -> 14:00-20:00
    UTC) debe quedar con Data912RestSource; uno a las 15:00 UTC (12:00 ART,
    sigue dentro de la rueda) debe quedar con MockReplaySource Y marcado
    within_byma_session=True - exactamente el caso mas grave que pidio el
    usuario ("incluidos los de horario de rueda").
    """
    log_path = tmp_path / "ggal_bot.log"
    _write_log(log_path, [
        "2026-09-29 10:00:00,000 [INFO] ggal_bot.data.live_shadow_feed: Shadow feed: fuente activa = 'Data912RestSource' (prioridad 1/2).",
        "2026-09-29 11:30:00,000 [WARNING] ggal_bot.data.live_shadow_feed: Shadow feed: failover (fallaron 3 polls consecutivos) -> fuente 'MockReplaySource' (prioridad 2/2).",
    ])

    shadow_trades_path = tmp_path / "shadow_trades.csv"
    shadow_trades_path.write_text("\n".join([
        _SHADOW_TRADES_HEADER,
        _fill_row("2026-09-29T14:00:00+00:00", "cid-real"),
        _fill_row("2026-09-29T15:00:00+00:00", "cid-mock-in-session"),
        _fill_row("2026-09-30T03:00:00+00:00", "cid-mock-out-of-session"),  # madrugada, fuera de rueda
    ]) + "\n", encoding="utf-8")

    rows, misconfig_warnings = qmf.quantify([log_path], shadow_trades_path)
    assert misconfig_warnings == []
    by_id = {r.client_order_id: r for r in rows}

    assert by_id["cid-real"].active_source == "Data912RestSource"
    assert by_id["cid-real"].within_byma_session is True

    assert by_id["cid-mock-in-session"].active_source == "MockReplaySource"
    assert by_id["cid-mock-in-session"].within_byma_session is True

    assert by_id["cid-mock-out-of-session"].active_source == "MockReplaySource"
    assert by_id["cid-mock-out-of-session"].within_byma_session is False

    report = qmf.format_report(rows, misconfig_warnings)
    assert "Data912RestSource: 1 fill(s)" in report
    assert "MockReplaySource: 2 fill(s)" in report
    assert "DURANTE el horario de rueda asumido" in report
    assert "1" in report.splitlines()[next(i for i, l in enumerate(report.splitlines()) if "DURANTE el horario" in l)]


def test_quantify_marks_fills_before_any_known_transition_as_unknown(tmp_path):
    log_path = tmp_path / "ggal_bot.log"
    _write_log(log_path, [
        "2026-09-30 10:00:00,000 [INFO] ggal_bot.data.live_shadow_feed: Shadow feed: fuente activa = 'Data912RestSource' (prioridad 1/2).",
    ])
    shadow_trades_path = tmp_path / "shadow_trades.csv"
    shadow_trades_path.write_text("\n".join([
        _SHADOW_TRADES_HEADER,
        _fill_row("2026-09-29T10:00:00+00:00", "cid-earlier-than-any-log"),
    ]) + "\n", encoding="utf-8")

    rows, _ = qmf.quantify([log_path], shadow_trades_path)
    assert rows[0].active_source == qmf._UNKNOWN_SOURCE
