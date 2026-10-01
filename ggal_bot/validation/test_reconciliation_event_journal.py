"""
test_reconciliation_event_journal.py
=====================================
Tests de regresion para
ggal_bot/portfolio/reconciliation.py::reconstruct_positions_from_event_journal
(Tarea #27/#28, a pedido explicito del usuario, sesion 2026-10-01: "no
copies el parche del dashboard. Reconstrui las posiciones desde el event
journal (position_id + strategy_tag)... Tests de regresion con los casos
GFGC6600OC y GFGC6400OC").

Los casos GFGC6600OC/GFGC6400OC de abajo reproducen la ESTRUCTURA real
verificada contra logs/position_events.csv de produccion (hasta 2026-10-01
17:08 UTC) - mismos position_id/timestamps/precios/cantidades que las filas
reales para esos dos simbolos (ver el reporte de la Tarea #27 en el chat de
esta sesion), no datos inventados.

Correr con:
    python -m pytest ggal_bot/validation/test_reconciliation_event_journal.py
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

pytest.importorskip("pandas")

from ggal_bot.portfolio.reconciliation import reconstruct_positions_from_event_journal

_HEADER = [
    "timestamp_utc", "event_type", "position_id", "contract_key",
    "symbol", "strategy_tag", "side", "quantity_delta", "quantity_after",
    "price", "order_client_id", "reason", "data_unavailable_fields",
]


def _write_journal(path: Path, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_HEADER)
        for row in rows:
            w.writerow([row.get(col, "") for col in _HEADER])


def _ev(ts, event_type, position_id, symbol, strategy_tag, side, delta, after, price, reason=""):
    return {
        "timestamp_utc": ts, "event_type": event_type, "position_id": position_id,
        "symbol": symbol, "strategy_tag": strategy_tag, "side": side,
        "quantity_delta": delta, "quantity_after": after, "price": price, "reason": reason,
    }


# ---------------------------------------------------------------------------
# Caso real GFGC6600OC: weekly_asymmetric (ENTRY 16 -> CLOSE -16, neta en 0)
# contaminado por 3 reducciones "huerfanas" el 2026-09-30 (-4,-2,-1) que NO
# matchean ningun lote abierto de weekly_asymmetric en el journal - mas el
# lote de scalping (ENTRY +8@93.0) que quedo abierto en paralelo. Esto es
# EXACTAMENTE lo que el bug viejo (reconstruct_positions_from_shadow_log,
# ciego a strategy_tag) blendeaba en una sola Position qty=1.0 mal
# etiquetada "weekly_asymmetric".
# ---------------------------------------------------------------------------

def _gfgc6600oc_rows():
    return [
        _ev("2026-09-08T17:51:59.949503+00:00", "ENTRY", "23cc49b7d042", "GFGC6600OC",
            "weekly_asymmetric", "buy", 2.0, 2.0, 667.5),
        _ev("2026-09-09T17:48:52.294447+00:00", "PARTIAL_EXIT", "ac1912b8e4ac", "GFGC6600OC",
            "weekly_asymmetric", "sell", -1.0, 1.0, 769.4395, "partial_profit_take"),
        _ev("2026-09-11T13:45:37.056685+00:00", "CLOSE", "99a2ff46c818", "GFGC6600OC",
            "weekly_asymmetric", "sell", -1.0, 0.0, 739.855, "weekend_theta_guard"),
        _ev("2026-09-28T14:05:53.013922+00:00", "ENTRY", "8073aff38d40", "GFGC6600OC",
            "weekly_asymmetric", "buy", 16.0, 16.0, 119.0),
        _ev("2026-09-29T13:28:32.945782+00:00", "CLOSE", "afed1e5f148c", "GFGC6600OC",
            "weekly_asymmetric", "sell", -16.0, 0.0, 382.0, "take_profit"),
        _ev("2026-09-30T15:24:12.910511+00:00", "ENTRY", "d5f938ac2d39", "GFGC6600OC",
            "scalping", "buy", 8.0, 8.0, 93.0),
        _ev("2026-09-30T17:52:07.077280+00:00", "PARTIAL_EXIT", "4b1225de47e0", "GFGC6600OC",
            "weekly_asymmetric", "sell", -4.0, 4.0, 109.3955, "partial_profit_take"),
        _ev("2026-09-30T18:06:41.249066+00:00", "PARTIAL_EXIT", "afc79b934282", "GFGC6600OC",
            "weekly_asymmetric", "sell", -2.0, 2.0, 117.005, "partial_profit_take"),
        _ev("2026-09-30T18:06:54.067395+00:00", "PARTIAL_EXIT", "5358b6c078ed", "GFGC6600OC",
            "weekly_asymmetric", "sell", -1.0, 1.0, 117.05, "partial_profit_take"),
    ]


def test_gfgc6600oc_weekly_asymmetric_nets_to_zero_not_blended_with_scalping(tmp_path):
    """
    El fix central de la Tarea #27: weekly_asymmetric en GFGC6600OC debe
    leerse como CERRADA (neto 0, el ENTRY 16 -> CLOSE -16 cierra el unico
    lote real), NUNCA como qty=1.0 (el numero que daba el bug viejo al
    blendear el lote de scalping con el remanente contaminado de
    weekly_asymmetric).
    """
    path = tmp_path / "position_events.csv"
    _write_journal(path, _gfgc6600oc_rows())

    positions, warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)

    weekly = [p for p in positions if p.symbol == "GFGC6600OC" and (p.strategy_tag or "weekly_asymmetric") == "weekly_asymmetric"]
    assert weekly == [], f"weekly_asymmetric deberia estar en 0 (cerrada), no: {weekly}"


def test_gfgc6600oc_scalping_lot_correctly_isolated(tmp_path):
    """El lote de scalping (+8@93.0) debe aparecer COMPLETO y correctamente
    etiquetado, sin que las reducciones huerfanas de weekly_asymmetric lo toquen."""
    path = tmp_path / "position_events.csv"
    _write_journal(path, _gfgc6600oc_rows())

    positions, _warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)

    scalping = [p for p in positions if p.symbol == "GFGC6600OC" and p.strategy_tag == "scalping"]
    assert len(scalping) == 1, f"se esperaba exactamente 1 posicion de scalping, hubo: {scalping}"
    pos = scalping[0]
    assert pos.quantity == pytest.approx(8.0)
    assert pos.entry_price == pytest.approx(93.0)
    assert pos.position_id == "d5f938ac2d39"


def test_gfgc6600oc_orphan_reductions_are_reported_never_silently_dropped(tmp_path):
    """
    Las 3 reducciones de weekly_asymmetric del 2026-09-30 (-4,-2,-1) no
    matchean ningun lote abierto de weekly_asymmetric en el journal (su
    unico lote real, ENTRY 8073aff38d40, ya aparece CERRADO un dia antes) -
    deben quedar reportadas en `warnings`, nunca fabricar una resolucion
    (ni "pertenecen a scalping" ni "son un nuevo lote fantasma").
    """
    path = tmp_path / "position_events.csv"
    _write_journal(path, _gfgc6600oc_rows())

    _positions, warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)

    orphan_warnings = [w for w in warnings if "no se pudo asociar a ningun lote abierto" in w]
    assert len(orphan_warnings) == 3, f"se esperaban 3 avisos de reduccion huerfana, hubo {len(orphan_warnings)}: {warnings}"
    for pid in ("4b1225de47e0", "afc79b934282", "5358b6c078ed"):
        assert any(pid in w for w in orphan_warnings), f"falta el aviso para position_id={pid}"


# ---------------------------------------------------------------------------
# Caso real GFGC6400OC: weekly_asymmetric (ENTRY 12 -> PARTIAL -6 -> CLOSE
# -6, neta en 0 EXACTA, sin huerfanos) + scalping con varios round-trips
# intradiarios cerrados y un ULTIMO lote +7@101.9955 que queda abierto.
# ---------------------------------------------------------------------------

def _gfgc6400oc_rows():
    return [
        _ev("2026-09-28T14:12:56.411510+00:00", "ENTRY", "63048cc128e6", "GFGC6400OC",
            "scalping", "buy", 4.0, 4.0, 177.5),
        _ev("2026-09-28T14:13:40.294067+00:00", "CLOSE", "63048cc128e6", "GFGC6400OC",
            "scalping", "sell", -4.0, 0.0, 175.005, "scalping_iv_mean_reversion"),
        _ev("2026-09-28T15:51:13.527899+00:00", "ENTRY", "041904d6761c", "GFGC6400OC",
            "weekly_asymmetric", "buy", 12.0, 12.0, 158.063),
        _ev("2026-09-29T13:23:52.675311+00:00", "PARTIAL_EXIT", "e3b6cc21bd83", "GFGC6400OC",
            "weekly_asymmetric", "sell", -6.0, 6.0, 200.0, "partial_profit_take"),
        _ev("2026-09-29T15:21:17.065034+00:00", "CLOSE", "3b31784a4b96", "GFGC6400OC",
            "weekly_asymmetric", "sell", -6.0, 0.0, 101.6655, "stop_loss"),
        _ev("2026-09-29T15:31:42.763537+00:00", "ENTRY", "969e6d4fb448", "GFGC6400OC",
            "scalping", "buy", 7.0, 7.0, 111.1655),
        _ev("2026-09-29T15:32:03.389404+00:00", "CLOSE", "969e6d4fb448", "GFGC6400OC",
            "scalping", "sell", -7.0, 0.0, 111.1655, "scalping_iv_mean_reversion"),
        _ev("2026-10-01T14:07:43.634542+00:00", "ENTRY", "ba214b455009", "GFGC6400OC",
            "scalping", "buy", 7.0, 7.0, 101.9955),
    ]


def test_gfgc6400oc_weekly_asymmetric_clean_round_trip_nets_to_zero_no_orphans(tmp_path):
    path = tmp_path / "position_events.csv"
    _write_journal(path, _gfgc6400oc_rows())

    positions, warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)

    weekly = [p for p in positions if p.symbol == "GFGC6400OC" and (p.strategy_tag or "weekly_asymmetric") == "weekly_asymmetric"]
    assert weekly == []
    assert not any("GFGC6400OC" in w and "no se pudo asociar" in w for w in warnings)


def test_gfgc6400oc_only_the_last_open_scalping_lot_survives(tmp_path):
    path = tmp_path / "position_events.csv"
    _write_journal(path, _gfgc6400oc_rows())

    positions, _warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)

    gfgc6400 = [p for p in positions if p.symbol == "GFGC6400OC"]
    assert len(gfgc6400) == 1, f"se esperaba una sola Position (el ultimo lote de scalping todavia abierto): {gfgc6400}"
    pos = gfgc6400[0]
    assert pos.strategy_tag == "scalping"
    assert pos.quantity == pytest.approx(7.0)
    assert pos.entry_price == pytest.approx(101.9955)
    assert pos.position_id == "ba214b455009"


# ---------------------------------------------------------------------------
# Fix del "BUG B" (Tarea #28): position_id ya NO se reinventa en cada
# reconciliacion - reconciliar dos veces el MISMO journal (equivalente a dos
# restarts sucesivos sin eventos nuevos en el medio) debe devolver SIEMPRE
# el mismo position_id.
# ---------------------------------------------------------------------------

def test_position_id_is_stable_across_repeated_reconciliation_restart_simulation(tmp_path):
    path = tmp_path / "position_events.csv"
    _write_journal(path, [
        _ev("2026-09-28T14:05:53+00:00", "ENTRY", "8073aff38d40", "GFGC6600OC",
            "weekly_asymmetric", "buy", 16.0, 16.0, 119.0),
    ])

    positions_1, _ = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)
    positions_2, _ = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)

    assert len(positions_1) == 1 and len(positions_2) == 1
    assert positions_1[0].position_id == "8073aff38d40"
    assert positions_2[0].position_id == "8073aff38d40"
    assert positions_1[0].position_id == positions_2[0].position_id, (
        "BUG B: dos reconciliaciones sucesivas del mismo journal no deberian "
        "fabricar identidades distintas para la misma posicion."
    )


def test_position_id_follows_the_most_recent_journal_event_once_reduced(tmp_path):
    """
    Si, DESPUES de una primera reconciliacion, el bot reduce la posicion
    (en memoria, usando el position_id que la reconciliacion le dio) y
    vuelve a reiniciar, la SEGUNDA reconciliacion debe seguir viendo la
    MISMA identidad (la del evento mas reciente), no una nueva.
    """
    path = tmp_path / "position_events.csv"
    _write_journal(path, [
        _ev("2026-09-28T14:05:53+00:00", "ENTRY", "8073aff38d40", "GFGC6600OC",
            "weekly_asymmetric", "buy", 16.0, 16.0, 119.0),
    ])
    positions_1, _ = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)
    assert positions_1[0].position_id == "8073aff38d40"

    # El bot, ya reconciliado, reduce la posicion y loguea el REDUCE con el
    # MISMO position_id que la reconciliacion le dio (comportamiento
    # correcto, a diferencia del bug historico) - se agrega esa fila al
    # journal y se reconcilia de nuevo (simula un segundo restart).
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        row = _ev("2026-09-30T10:00:00+00:00", "PARTIAL_EXIT", "8073aff38d40", "GFGC6600OC",
                   "weekly_asymmetric", "sell", -6.0, 10.0, 150.0, "partial_profit_take")
        w.writerow([row.get(col, "") for col in _HEADER])

    positions_2, _ = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)
    assert len(positions_2) == 1
    assert positions_2[0].quantity == pytest.approx(10.0)
    assert positions_2[0].position_id == "8073aff38d40"


# ---------------------------------------------------------------------------
# Casos base / de borde
# ---------------------------------------------------------------------------

def test_short_wing_of_a_spread_reconstructs_with_negative_quantity(tmp_path):
    """
    Caso real GFGV5000OC (pata corta de spread_completion, ver Tarea #27
    item 3): ENTRY con quantity_delta NEGATIVO (sell_to_open_wing) debe
    reconstruirse como una Position con cantidad negativa, no como un
    'huerfano' (el FIFO generico es direccion-agnostico: el signo del
    primer evento abre el lote, sea cual sea).
    """
    path = tmp_path / "position_events.csv"
    _write_journal(path, [
        _ev("2026-10-01T10:47:59+00:00", "ENTRY", "ef268fed9a99", "GFGV5000OC",
            "weekly_asymmetric", "sell", -75.0, -75.0, 6.1205, "spread_completion"),
    ])

    positions, warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)

    assert len(positions) == 1
    pos = positions[0]
    assert pos.quantity == pytest.approx(-75.0)
    assert pos.entry_price == pytest.approx(6.1205)
    assert not any("no se pudo asociar" in w for w in warnings)


def test_reject_events_are_ignored_entirely(tmp_path):
    path = tmp_path / "position_events.csv"
    _write_journal(path, [
        _ev("2026-09-01T10:00:00+00:00", "REJECT", "", "GFGC7000OC", "weekly_asymmetric",
            "buy", "", "", "", "greeks_limit_exceeded: {}"),
        _ev("2026-09-01T10:05:00+00:00", "ENTRY", "p1", "GFGC7000OC", "weekly_asymmetric",
            "buy", 2.0, 2.0, 500.0),
    ])
    positions, _warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)
    assert len(positions) == 1
    assert positions[0].quantity == pytest.approx(2.0)


def test_orphan_reduction_with_no_prior_entry_at_all_is_reported(tmp_path):
    """Posicion legacy (anterior al deploy del journal): una reduccion sin
    NINGUN ENTRY previo para ese (symbol, strategy_tag) - debe reportarse,
    nunca crashear ni producir una Position con cantidad fabricada."""
    path = tmp_path / "position_events.csv"
    _write_journal(path, [
        _ev("2026-09-07T18:00:00+00:00", "CLOSE", "legacy1", "GFGC9999OC", "weekly_asymmetric",
            "sell", -5.0, 0.0, 300.0, "legacy_close"),
    ])
    positions, warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)
    assert positions == []
    assert any("GFGC9999OC" in w and "no se pudo asociar" in w for w in warnings)


def test_empty_journal_returns_no_positions(tmp_path):
    path = tmp_path / "position_events.csv"
    _write_journal(path, [])
    positions, warnings = reconstruct_positions_from_event_journal(csv_path=path, option_multiplier=100.0)
    assert positions == []
    assert warnings == []


def test_missing_journal_file_returns_empty_without_error(tmp_path):
    positions, warnings = reconstruct_positions_from_event_journal(csv_path=tmp_path / "no_existe.csv")
    assert positions == []
    assert warnings == []
