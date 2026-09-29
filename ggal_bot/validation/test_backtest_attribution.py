"""
test_backtest_attribution.py
===============================
Tests para ggal_bot/backtest/attribution.py: diagnostico del PnL BRUTO por
motivo de salida, moneyness, dias al vencimiento (DTE), tiempo de tenencia
y hora de entrada. Usa Trade sinteticos (nunca datos reales del usuario)
para verificar cada regla de bucketing contra un calculo manual exacto,
incluyendo los casos DATA INSUFFICIENT (trade excluido, no fabricado).

Correr con:
    python -m ggal_bot.validation.test_backtest_attribution
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ggal_bot.backtest.attribution import (
    attribute_by_close_reason,
    attribute_by_dte,
    attribute_by_entry_hour_art,
    attribute_by_holding_time,
    attribute_by_moneyness,
    bucket_close_reason,
    load_spot_closes_csv,
    nearest_close_on_or_before,
    parse_contract_key_expiry,
    parse_option_symbol,
    unmapped_close_reasons,
)
from ggal_bot.backtest.reconstruct import Leg, Trade
from datetime import date


def _trade(symbol="GFGC7000OC", opened="2026-09-10T14:00:00+00:00", closed="2026-09-11T14:00:00+00:00",
           pnl=1000.0, close_reason=None, contract_key=None, multiplier=100.0) -> Trade:
    opened_dt = datetime.fromisoformat(opened) if opened else None
    closed_dt = datetime.fromisoformat(closed) if closed else None
    return Trade(
        strategy="weekly_asymmetric", symbol=symbol, trade_id=f"{symbol}_{opened}",
        opened_at=opened_dt, closed_at=closed_dt, multiplier=multiplier,
        entry_legs=[Leg(quantity=1, price=100.0, timestamp=opened_dt)],
        exit_legs=[Leg(quantity=1, price=110.0, timestamp=closed_dt)],
        pnl_gross_ars=pnl, close_reason=close_reason, contract_key=contract_key,
    )


def test_bucket_close_reason_maps_known_reasons():
    assert bucket_close_reason("stop_loss") == "stop"
    assert bucket_close_reason("scalping_take_profit") == "take_profit"
    assert bucket_close_reason("scalping_iv_mean_reversion") == "take_profit"
    assert bucket_close_reason("weekend_theta_guard") == "timeout"
    assert bucket_close_reason("scalping_eod_close") == "timeout"


def test_bucket_close_reason_unknown_falls_back_to_otro():
    assert bucket_close_reason("algo_nunca_visto") == "otro"
    assert bucket_close_reason(None) == "otro"


def test_unmapped_close_reasons_reports_only_unknown_ones():
    trades = [
        _trade(close_reason="stop_loss"),
        _trade(close_reason="motivo_nuevo_no_mapeado"),
        _trade(close_reason="motivo_nuevo_no_mapeado"),
    ]
    unmapped = unmapped_close_reasons(trades)
    assert unmapped == {"motivo_nuevo_no_mapeado": 2}


def test_parse_option_symbol_call_and_put():
    assert parse_option_symbol("GFGC7000OC") == ("call", 7000.0)
    assert parse_option_symbol("GFGV6400I") == ("put", 6400.0)
    assert parse_option_symbol("no_es_una_opcion") is None


def test_parse_contract_key_expiry_valid_and_invalid():
    assert parse_contract_key_expiry("GGAL|GFGC7000OC|2026-10-16") == date(2026, 10, 16)
    assert parse_contract_key_expiry(None) is None
    assert parse_contract_key_expiry("formato_invalido") is None
    assert parse_contract_key_expiry("GGAL|GFGC7000OC|fecha_mala") is None


def test_load_spot_closes_csv_and_nearest_lookup():
    fd, name = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    path = Path(name)
    path.write_text(
        "# comentario de fuente, debe ignorarse\n"
        "date,open,high,low,close,volume\n"
        "2026-09-04,7075,7125,6950,7025,749800\n"
        "2026-09-07,7040,7070,6870,6930,267463\n",
        encoding="utf-8",
    )
    try:
        spot = load_spot_closes_csv(path)
        assert spot[date(2026, 9, 4)] == 7025.0
        # 2026-09-05 y 06 son fin de semana (sin barra) - debe encontrar el viernes 09-04 hacia atras.
        assert nearest_close_on_or_before(spot, date(2026, 9, 6)) == 7025.0
        # Nunca busca HACIA ADELANTE: una fecha anterior a cualquier dato disponible da None.
        assert nearest_close_on_or_before(spot, date(2026, 9, 1)) is None
    finally:
        path.unlink(missing_ok=True)


def test_attribute_by_close_reason_excludes_trades_without_reason():
    trades = [
        _trade(close_reason="stop_loss", pnl=-500.0),
        _trade(close_reason="scalping_eod_close", pnl=300.0),
        _trade(close_reason=None, pnl=999.0),  # ej. vol_arbitrage - sin motivo, debe excluirse
    ]
    buckets = attribute_by_close_reason(trades)
    total_n = sum(b.n for b in buckets)
    assert total_n == 2  # el trade sin close_reason no aparece en ningun bucket
    by_label = {b.label: b for b in buckets}
    assert by_label["stop"].gross_pnl_sum_ars == -500.0
    assert by_label["timeout"].gross_pnl_sum_ars == 300.0


def test_attribute_by_moneyness_call_itm_and_otm():
    spot_by_date = {date(2026, 9, 10): 7200.0}  # spot bien por encima del strike de la call
    trades = [
        _trade(symbol="GFGC7000OC", opened="2026-09-10T14:00:00+00:00"),  # call ITM: (7200-7000)/7000 = +2.86% -> "ITM 2-5%"
        _trade(symbol="GFGC8000OC", opened="2026-09-10T14:00:00+00:00"),  # call OTM: (7200-8000)/8000 = -10% -> "OTM >5%"
    ]
    buckets = {b.label: b for b in attribute_by_moneyness(trades, spot_by_date)}
    assert "ITM 2-5%" in buckets
    assert "OTM >5%" in buckets


def test_attribute_by_moneyness_put_has_reversed_sign():
    spot_by_date = {date(2026, 9, 10): 6800.0}
    # Put strike 7000, spot 6800: para un put, ITM significa spot < strike.
    # raw = (6800-7000)/7000 = -2.86%; para un put se invierte el signo -> +2.86% (ITM).
    trades = [_trade(symbol="GFGV7000I", opened="2026-09-10T14:00:00+00:00")]
    buckets = {b.label: b for b in attribute_by_moneyness(trades, spot_by_date)}
    assert "ITM 2-5%" in buckets


def test_attribute_by_moneyness_excludes_unparseable_symbol_or_missing_spot():
    trades = [
        _trade(symbol="TICKER_RARO", opened="2026-09-10T14:00:00+00:00"),
        _trade(symbol="GFGC7000OC", opened="2026-01-01T14:00:00+00:00"),  # fuera de rango del spot disponible
    ]
    buckets = attribute_by_moneyness(trades, {date(2026, 9, 10): 7200.0})
    assert sum(b.n for b in buckets) == 0


def test_attribute_by_dte_buckets_and_excludes_missing_contract_key():
    trades = [
        _trade(opened="2026-09-10T14:00:00+00:00", contract_key="GGAL|GFGC7000OC|2026-09-12"),  # 2 dias -> "0-3d"
        _trade(opened="2026-09-10T14:00:00+00:00", contract_key="GGAL|GFGC7000OC|2026-09-25"),  # 15 dias -> "15+d"
        _trade(opened="2026-09-10T14:00:00+00:00", contract_key=None),  # vol_arbitrage: sin contract_key -> excluido
    ]
    buckets = {b.label: b for b in attribute_by_dte(trades)}
    assert buckets["0-3d"].n == 1
    assert buckets["15+d"].n == 1
    assert sum(b.n for b in buckets.values()) == 2  # el trade sin contract_key nunca aparece


def test_attribute_by_holding_time_buckets():
    trades = [
        _trade(opened="2026-09-10T10:00:00+00:00", closed="2026-09-10T10:30:00+00:00"),  # 30min -> "<1h"
        _trade(opened="2026-09-10T10:00:00+00:00", closed="2026-09-12T10:00:00+00:00"),  # 2 dias -> "1-3d"
    ]
    buckets = {b.label: b for b in attribute_by_holding_time(trades)}
    assert buckets["<1h"].n == 1
    assert buckets["1-3d"].n == 1


def test_attribute_by_entry_hour_art_converts_from_utc():
    # 14:00 UTC = 11:00 ART (UTC-3).
    trades = [_trade(opened="2026-09-10T14:00:00+00:00")]
    buckets = attribute_by_entry_hour_art(trades)
    assert len(buckets) == 1
    assert buckets[0].label == "11h ART"


ALL_TESTS = [
    test_bucket_close_reason_maps_known_reasons,
    test_bucket_close_reason_unknown_falls_back_to_otro,
    test_unmapped_close_reasons_reports_only_unknown_ones,
    test_parse_option_symbol_call_and_put,
    test_parse_contract_key_expiry_valid_and_invalid,
    test_load_spot_closes_csv_and_nearest_lookup,
    test_attribute_by_close_reason_excludes_trades_without_reason,
    test_attribute_by_moneyness_call_itm_and_otm,
    test_attribute_by_moneyness_put_has_reversed_sign,
    test_attribute_by_moneyness_excludes_unparseable_symbol_or_missing_spot,
    test_attribute_by_dte_buckets_and_excludes_missing_contract_key,
    test_attribute_by_holding_time_buckets,
    test_attribute_by_entry_hour_art_converts_from_utc,
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
