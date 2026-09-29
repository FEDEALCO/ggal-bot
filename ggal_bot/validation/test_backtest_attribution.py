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
    is_friday_entry_weekend_guard_trade,
    load_spot_closes_csv,
    nearest_close_on_or_before,
    parse_contract_key_expiry,
    parse_option_symbol,
    split_by_holding_business_days_cutoff,
    split_friday_weekend_guard_trades,
    trade_holding_business_days,
    unmapped_close_reasons,
    winner_loser_holding_profile,
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


def test_winner_loser_holding_profile_computes_median_holding_and_pnl_per_group():
    trades = [
        _trade(opened="2026-09-10T10:00:00+00:00", closed="2026-09-10T10:10:00+00:00", pnl=100.0),   # ganadora, 10min
        _trade(opened="2026-09-11T10:00:00+00:00", closed="2026-09-11T10:20:00+00:00", pnl=200.0),   # ganadora, 20min
        _trade(opened="2026-09-12T10:00:00+00:00", closed="2026-09-15T10:00:00+00:00", pnl=-50.0),   # perdedora, 3 dias
        _trade(opened="2026-09-13T10:00:00+00:00", closed="2026-09-20T10:00:00+00:00", pnl=-150.0),  # perdedora, 7 dias
    ]
    profile = winner_loser_holding_profile(trades)
    assert profile.n_winners == 2
    assert profile.n_losers == 2
    assert profile.median_holding_seconds_winners == 900.0  # mediana de 600s y 1200s
    assert profile.median_holding_seconds_losers == 432000.0  # mediana de 3 dias (259200s) y 7 dias (604800s)
    assert profile.mean_gross_pnl_winners_ars == 150.0
    assert profile.mean_gross_pnl_losers_ars == -100.0


def test_winner_loser_holding_profile_excludes_zero_pnl_and_missing_holding():
    trades = [
        _trade(opened="2026-09-10T10:00:00+00:00", closed="2026-09-10T10:10:00+00:00", pnl=0.0),  # ni ganadora ni perdedora
        _trade(opened=None, closed=None, pnl=50.0),  # ganadora sin fechas -> cuenta en n_winners pero no en la mediana de tenencia
    ]
    profile = winner_loser_holding_profile(trades)
    assert profile.n_winners == 1
    assert profile.n_losers == 0
    assert profile.median_holding_seconds_winners is None


def test_attribute_by_entry_hour_art_converts_from_utc():
    # 14:00 UTC = 11:00 ART (UTC-3).
    trades = [_trade(opened="2026-09-10T14:00:00+00:00")]
    buckets = attribute_by_entry_hour_art(trades)
    assert len(buckets) == 1
    assert buckets[0].label == "11h ART"


def test_is_friday_entry_weekend_guard_trade_true_when_both_conditions_hold():
    """2026-09-11 14:00 UTC = 11:00 ART, viernes; close_reason es exactamente 'weekend_theta_guard'."""
    t = _trade(opened="2026-09-11T14:00:00+00:00", close_reason="weekend_theta_guard")
    assert is_friday_entry_weekend_guard_trade(t) is True


def test_is_friday_entry_weekend_guard_trade_false_if_close_reason_differs():
    """Mismo viernes, pero cerrado por otro motivo (ej. stop_loss) - no es el patron 'viernes flash'."""
    t = _trade(opened="2026-09-11T14:00:00+00:00", close_reason="stop_loss")
    assert is_friday_entry_weekend_guard_trade(t) is False


def test_is_friday_entry_weekend_guard_trade_false_if_not_friday():
    """Jueves 2026-09-10, mismo close_reason - el dia de la semana es la otra condicion necesaria."""
    t = _trade(opened="2026-09-10T14:00:00+00:00", close_reason="weekend_theta_guard")
    assert is_friday_entry_weekend_guard_trade(t) is False


def test_is_friday_entry_weekend_guard_trade_uses_art_not_utc_at_day_boundary():
    """
    2026-09-12T01:00:00+00:00 (sabado en UTC) es 2026-09-11T22:00:00 ART
    (viernes de noche) - la conversion a ART (no UTC crudo) es la que
    importa para juzgar el dia de la semana de la entrada.
    """
    t = _trade(opened="2026-09-12T01:00:00+00:00", close_reason="weekend_theta_guard")
    assert is_friday_entry_weekend_guard_trade(t) is True


def test_is_friday_entry_weekend_guard_trade_false_without_opened_at():
    t = _trade(opened=None, close_reason="weekend_theta_guard")
    assert is_friday_entry_weekend_guard_trade(t) is False


def test_trade_holding_business_days_counts_weekdays_only():
    """Jueves 2026-09-10 -> lunes 2026-09-14: 2 dias habiles (viernes y lunes; sabado/domingo no cuentan)."""
    t = _trade(opened="2026-09-10T14:00:00+00:00", closed="2026-09-14T14:00:00+00:00")
    assert trade_holding_business_days(t) == 2


def test_trade_holding_business_days_same_day_is_zero():
    t = _trade(opened="2026-09-10T11:00:00+00:00", closed="2026-09-10T18:00:00+00:00")
    assert trade_holding_business_days(t) == 0


def test_trade_holding_business_days_none_without_both_timestamps():
    assert trade_holding_business_days(_trade(opened=None)) is None
    assert trade_holding_business_days(_trade(closed=None)) is None


def test_split_by_holding_business_days_cutoff_partitions_exhaustively():
    trades = [
        _trade(symbol="A", opened="2026-09-10T14:00:00+00:00", closed="2026-09-10T18:00:00+00:00"),  # 0 dias
        _trade(symbol="B", opened="2026-09-10T14:00:00+00:00", closed="2026-09-11T14:00:00+00:00"),  # 1 dia
        _trade(symbol="C", opened="2026-09-10T14:00:00+00:00", closed="2026-09-14T14:00:00+00:00"),  # 2 dias
        _trade(symbol="D", opened=None, closed="2026-09-14T14:00:00+00:00"),                          # sin fecha
    ]
    within, beyond, unknown = split_by_holding_business_days_cutoff(trades, cutoff_business_days=1)
    assert {t.symbol for t in within} == {"A", "B"}
    assert {t.symbol for t in beyond} == {"C"}
    assert {t.symbol for t in unknown} == {"D"}
    assert len(within) + len(beyond) + len(unknown) == len(trades)


def test_split_friday_weekend_guard_trades_partitions_exhaustively():
    trades = [
        _trade(symbol="A", opened="2026-09-11T14:00:00+00:00", close_reason="weekend_theta_guard"),  # flash
        _trade(symbol="B", opened="2026-09-11T14:00:00+00:00", close_reason="stop_loss"),             # resto (viernes pero no ese motivo)
        _trade(symbol="C", opened="2026-09-10T14:00:00+00:00", close_reason="weekend_theta_guard"),   # resto (motivo pero no viernes)
        _trade(symbol="D", opened="2026-09-18T14:00:00+00:00", close_reason="weekend_theta_guard"),   # flash (otro viernes)
    ]
    flash, rest = split_friday_weekend_guard_trades(trades)
    assert {t.symbol for t in flash} == {"A", "D"}
    assert {t.symbol for t in rest} == {"B", "C"}
    assert len(flash) + len(rest) == len(trades)


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
    test_winner_loser_holding_profile_computes_median_holding_and_pnl_per_group,
    test_winner_loser_holding_profile_excludes_zero_pnl_and_missing_holding,
    test_attribute_by_entry_hour_art_converts_from_utc,
    test_is_friday_entry_weekend_guard_trade_true_when_both_conditions_hold,
    test_is_friday_entry_weekend_guard_trade_false_if_close_reason_differs,
    test_is_friday_entry_weekend_guard_trade_false_if_not_friday,
    test_is_friday_entry_weekend_guard_trade_uses_art_not_utc_at_day_boundary,
    test_is_friday_entry_weekend_guard_trade_false_without_opened_at,
    test_split_friday_weekend_guard_trades_partitions_exhaustively,
    test_trade_holding_business_days_counts_weekdays_only,
    test_trade_holding_business_days_same_day_is_zero,
    test_trade_holding_business_days_none_without_both_timestamps,
    test_split_by_holding_business_days_cutoff_partitions_exhaustively,
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
