"""
test_scalping_mode.py
========================
Tests de sanity para el modo ADITIVO "Scalping Intradia y Trading Semanal
de Corto Plazo" (ver config.ScalpingConfig y la nota de arquitectura junto
a esa clase):

    - models/microstructure.py       (passes_min_ask_depth)
    - data/iv_mean_reversion.py      (IVMeanReversionTracker: z-score,
                                        deteccion de reversion)
    - data/intraday_bars.py          (IntradayBarAggregator: agregacion de
                                        velas OHLC en memoria;
                                        MultiTimeframeIntradayEngine:
                                        confirmacion multi-timeframe)
    - risk/risk_manager.py           (evaluate_scalping_exit: Stop Loss /
                                        Take Profit / horizonte en minutos /
                                        falta de progreso / cierre EOD)
    - strategy/scalping.py           (ScalpingStrategy: reuso de
                                        WeeklyAsymmetricStrategy.scan_entry_signals
                                        por composicion, filtro de
                                        profundidad de ASK, glue de salidas)
    - AISLAMIENTO entre estrategias (portfolio.Position.strategy_tag):
      weekly_asymmetric y scalping NUNCA evaluan ni cierran la posicion de
      la otra, aunque compartan el mismo Portfolio - esto es lo que
      garantiza que la posicion de Octubre bajo weekly_asymmetric quede
      "como esta" cuando el modo scalping esta activo (ver run_bot.py).
    - run_bot.py:GgalOptionsBot                     (el modulo scalping es
                                                        un bolt-on: apagado
                                                        por defecto, no
                                                        cambia nada del
                                                        comportamiento
                                                        existente)

Correr con:
    python -m ggal_bot.validation.test_scalping_mode
"""

from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

# Debe importarse ANTES que run_bot/ggal_bot.execution.order_gateway (ver
# docstring de ese modulo) para redirigir el CSV de auditoria de shadow
# trading a un path temporal.
from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS, ScalpingConfig
from ggal_bot.data.intraday_bars import IntradayBarAggregator, MultiTimeframeIntradayEngine
from ggal_bot.data.iv_mean_reversion import IVMeanReversionTracker
from ggal_bot.data.option_chain import OptionQuote, OrderBookSnapshot
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.models.microstructure import passes_min_ask_depth
from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.portfolio.portfolio import Portfolio, Position
from ggal_bot.risk.risk_manager import RiskLimits, RiskManager
from ggal_bot.strategy.scalping import ScalpingStrategy
from ggal_bot.strategy.weekly_asymmetric import EntrySignal, WeeklyAsymmetricStrategy
from run_bot import GgalOptionsBot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lenient_risk_manager() -> RiskManager:
    return RiskManager(RiskLimits(
        max_vega_total=1e9, max_gamma_total=1e9,
        max_spread_relative=1.0, min_book_size=0.0, min_daily_volume=0.0,
    ))


def _scalping_config(**overrides) -> ScalpingConfig:
    cfg = ScalpingConfig(
        enabled=True,
        max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.08, min_contracts_per_trade=1,
        max_concurrent_positions=6,
        smile_threshold_vol_points=2.0, moneyness_band_pct=0.10, max_holding_business_days=3,
        require_level_confirmation=False, level_threshold_vol_points=5.0,
        enable_obi_filter=False, min_obi_for_entry=-0.15,
        enable_min_ask_depth_filter=True, min_ask_size_for_entry=30.0,
        stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=120.0, progress_check_minutes=30.0, min_progress_pnl_pct=0.05,
        eod_close_enabled=True, eod_close_time="16:50", eod_timezone_offset_hours=-3.0,
        enable_iv_mean_reversion_exit=True, iv_reversion_window_seconds=1800.0,
        iv_reversion_min_samples=5, iv_reversion_exit_zscore=0.5,
        fast_bar_interval_minutes=5, slow_bar_interval_minutes=15,
        require_multi_timeframe_agreement=True, max_bars_retained=300,
        refresh_interval_seconds=0.0, min_bars_required=25,
        ema_fast_period=9, ema_slow_period=21, rsi_period=9, adx_period=9,
        macd_fast_period=6, macd_slow_period=13, macd_signal_period=5, adx_trend_threshold=15.0,
        enable_momentum_shift_override=False, momentum_shift_lookback_bars=3, momentum_shift_rsi_delta=10.0,
    )
    for k, v in overrides.items():
        cfg = replace(cfg, **{k: v})
    return cfg


def _quote(symbol, strike, iv, spot_ref, days_biz, expiry=date(2026, 9, 4),
           option_type=OptionType.CALL, greeks=None, bid=95.0, ask=105.0,
           bid_size=100.0, ask_size=100.0):
    book = OrderBookSnapshot(symbol, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size, last_volume=1000.0)
    q = OptionQuote(symbol, strike=strike, expiry=expiry, option_type=option_type,
                     book=book, days_calendar=days_biz + 2, days_business=days_biz)
    q.iv = iv
    q.spot_ref = spot_ref
    q.greeks = greeks
    return q


def _cheap_calls_surface(spot=6600.0, days_biz=2, ask_size=50.0):
    """
    Smile simetrico con una base ATM claramente "barata" respecto de las
    alas (mismo patron que test_long_first_mode.py._default_config +
    test_scan_entry_signals_emits_buy_signal_for_cheap_base_in_band_and_horizon):
    GFGC6600O (moneyness ~0, IV 0.30) queda muy por debajo de la curva
    ajustada contra las 4 alas en 0.58-0.60 - eso es lo que genera una
    dislocacion negativa lo bastante grande como para calificar.
    """
    quotes = [
        _quote("GFGC6200O", 6200.0, 0.60, spot, days_biz, ask_size=ask_size),
        _quote("GFGC6400O", 6400.0, 0.58, spot, days_biz, ask_size=ask_size),
        _quote("GFGC6600O", 6600.0, 0.30, spot, days_biz, ask_size=ask_size),   # target: bien barata
        _quote("GFGC6800O", 6800.0, 0.58, spot, days_biz, ask_size=ask_size),
        _quote("GFGC7000O", 7000.0, 0.60, spot, days_biz, ask_size=ask_size),
    ]
    return VolatilitySurface(quotes)


# ---------------------------------------------------------------------------
# models/microstructure.py:passes_min_ask_depth
# ---------------------------------------------------------------------------

def test_passes_min_ask_depth_blocks_below_floor():
    book = OrderBookSnapshot("X", bid=10.0, ask=11.0, bid_size=100.0, ask_size=10.0)
    assert passes_min_ask_depth(book, 30.0) is False


def test_passes_min_ask_depth_allows_at_or_above_floor():
    book = OrderBookSnapshot("X", bid=10.0, ask=11.0, bid_size=100.0, ask_size=30.0)
    assert passes_min_ask_depth(book, 30.0) is True
    book2 = OrderBookSnapshot("X", bid=10.0, ask=11.0, bid_size=100.0, ask_size=50.0)
    assert passes_min_ask_depth(book2, 30.0) is True


def test_passes_min_ask_depth_blocks_when_no_ask_side_at_all():
    book = OrderBookSnapshot("X", bid=10.0, ask=11.0, bid_size=100.0, ask_size=0.0)
    assert passes_min_ask_depth(book, 1.0) is False


# ---------------------------------------------------------------------------
# data/iv_mean_reversion.py:IVMeanReversionTracker
# ---------------------------------------------------------------------------

def test_iv_tracker_zscore_none_without_enough_samples():
    tracker = IVMeanReversionTracker(min_samples=5)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        tracker.update("SYM", -3.0, now=now + timedelta(seconds=i))
    assert tracker.zscore("SYM") is None
    assert tracker.has_reverted("SYM", exit_abs_zscore=0.5) is False


def test_iv_tracker_detects_extreme_deviation_then_reversion():
    tracker = IVMeanReversionTracker(min_samples=5, max_window_seconds=3600.0)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    # Historia "normal" con algo de ruido real (desvio > 0) alrededor de -1.0
    baseline = [-1.0, -1.2, -0.8, -1.1, -0.9]
    for i, v in enumerate(baseline):
        tracker.update("SYM", v, now=now + timedelta(seconds=i))
    # Shock extremo: la dislocacion se va muy lejos del promedio reciente.
    tracker.update("SYM", -8.0, now=now + timedelta(seconds=10))
    z_extreme = tracker.zscore("SYM")
    assert z_extreme is not None and abs(z_extreme) > 2.0
    assert tracker.has_reverted("SYM", exit_abs_zscore=0.5) is False

    # Reversion: vuelve a moverse dentro del rango historico reciente.
    for i, v in enumerate([-1.0, -1.0, -1.0]):
        tracker.update("SYM", v, now=now + timedelta(seconds=20 + i))
    assert tracker.has_reverted("SYM", exit_abs_zscore=0.75) is True


def test_iv_tracker_ignores_none_dislocation():
    tracker = IVMeanReversionTracker(min_samples=1)
    tracker.update("SYM", None, now=datetime.now(timezone.utc))
    assert tracker.sample_count("SYM") == 0


def test_iv_tracker_trims_window_by_max_age():
    tracker = IVMeanReversionTracker(min_samples=1, max_window_seconds=10.0)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    tracker.update("SYM", -1.0, now=now)
    tracker.update("SYM", -1.0, now=now + timedelta(seconds=20))  # ya deberia expulsar la muestra vieja
    assert tracker.sample_count("SYM") == 1


# ---------------------------------------------------------------------------
# data/intraday_bars.py:IntradayBarAggregator
# ---------------------------------------------------------------------------

def test_intraday_bar_aggregator_builds_ohlc_within_same_bucket():
    agg = IntradayBarAggregator(interval_minutes=5)
    base = datetime(2026, 9, 3, 13, 0, 0, tzinfo=timezone.utc)
    agg.on_tick(base, 100.0)
    agg.on_tick(base + timedelta(minutes=1), 105.0)
    agg.on_tick(base + timedelta(minutes=2), 95.0)
    agg.on_tick(base + timedelta(minutes=3), 102.0)
    bars = agg.bars()
    assert len(bars) == 1  # todo cayo en el mismo bucket de 5 minutos -> 1 sola barra EN CURSO
    bar = bars[0]
    assert bar.open == 100.0
    assert bar.high == 105.0
    assert bar.low == 95.0
    assert bar.close == 102.0


def test_intraday_bar_aggregator_closes_bucket_on_next_interval():
    agg = IntradayBarAggregator(interval_minutes=5)
    base = datetime(2026, 9, 3, 13, 0, 0, tzinfo=timezone.utc)
    agg.on_tick(base, 100.0)
    agg.on_tick(base + timedelta(minutes=2), 110.0)
    agg.on_tick(base + timedelta(minutes=6), 120.0)  # nuevo bucket -> cierra el anterior
    bars = agg.bars()
    assert len(bars) == 2
    closed_bar, current_bar = bars[0], bars[1]
    assert closed_bar.open == 100.0 and closed_bar.close == 110.0
    assert current_bar.open == 120.0 and current_bar.close == 120.0


def test_intraday_bar_aggregator_ignores_invalid_price():
    agg = IntradayBarAggregator(interval_minutes=5)
    agg.on_tick(datetime.now(timezone.utc), None)
    agg.on_tick(datetime.now(timezone.utc), -5.0)
    assert agg.bar_count() == 0


def test_intraday_bar_aggregator_respects_max_bars_retained():
    agg = IntradayBarAggregator(interval_minutes=1, max_bars_retained=3)
    base = datetime(2026, 9, 3, 13, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        agg.on_tick(base + timedelta(minutes=i), 100.0 + i)
    # 10 buckets abiertos/cerrados en secuencia, se retienen a lo sumo 3 cerradas + 1 en curso.
    assert len(agg.bars()) <= 4


# ---------------------------------------------------------------------------
# data/intraday_bars.py:MultiTimeframeIntradayEngine
# ---------------------------------------------------------------------------

def _feed_uptrend(engine: MultiTimeframeIntradayEngine, n_bars: int, bar_minutes: int, start_price: float = 6000.0):
    base = datetime(2026, 9, 3, 13, 0, 0, tzinfo=timezone.utc)
    price = start_price
    for i in range(n_bars):
        ts = base + timedelta(minutes=i * bar_minutes)
        price += 15.0
        engine.on_tick(ts, price)
    return ts, price


def test_multi_timeframe_engine_combined_trend_neutral_without_enough_bars():
    cfg = _scalping_config(min_bars_required=25)
    engine = MultiTimeframeIntradayEngine(config=cfg)
    ts, _ = _feed_uptrend(engine, n_bars=5, bar_minutes=5)
    snapshot = engine.refresh(now=ts)
    assert snapshot.combined_trend == "NEUTRAL"


def test_multi_timeframe_engine_refresh_cache_respects_interval():
    cfg = _scalping_config(refresh_interval_seconds=3600.0, min_bars_required=5)
    engine = MultiTimeframeIntradayEngine(config=cfg)
    now = datetime(2026, 9, 3, 13, 0, 0, tzinfo=timezone.utc)
    engine.on_tick(now, 6600.0)
    first = engine.refresh(now=now)
    engine.on_tick(now + timedelta(seconds=5), 7000.0)  # movimiento fuerte, pero dentro del cache
    second = engine.refresh(now=now + timedelta(seconds=5))
    assert first is second  # cache vigente: no se recalculo


def test_multi_timeframe_engine_disagreement_forces_neutral():
    cfg = _scalping_config(
        min_bars_required=3, ema_fast_period=2, ema_slow_period=3, rsi_period=2, adx_period=2,
        macd_fast_period=2, macd_slow_period=3, macd_signal_period=2, adx_trend_threshold=0.0,
        fast_bar_interval_minutes=1, slow_bar_interval_minutes=60, refresh_interval_seconds=0.0,
        require_multi_timeframe_agreement=True,
    )
    engine = MultiTimeframeIntradayEngine(config=cfg)
    # El timeframe rapido (1m) recibe muchas barras alcistas; el lento (60m)
    # con el mismo stream de ticks (todos dentro de la MISMA hora) no
    # alcanza a formar mas de una barra -> no tiene lectura BULLISH/BEARISH
    # confirmada todavia -> el combinado debe degradar a NEUTRAL.
    base = datetime(2026, 9, 3, 13, 0, 0, tzinfo=timezone.utc)
    price = 6000.0
    for i in range(10):
        price += 20.0
        engine.on_tick(base + timedelta(minutes=i), price)
    snapshot = engine.refresh(now=base + timedelta(minutes=10), force=True)
    assert snapshot.combined_trend == "NEUTRAL"


# ---------------------------------------------------------------------------
# risk/risk_manager.py:evaluate_scalping_exit
# ---------------------------------------------------------------------------

def test_evaluate_scalping_exit_stop_loss():
    rm = _lenient_risk_manager()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=70.0, entry_time=now - timedelta(minutes=5), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=120.0, min_progress_pnl_pct=0.05, progress_check_minutes=30.0,
        eod_close_enabled=False,
    )
    assert reason == "scalping_stop_loss"


def test_evaluate_scalping_exit_take_profit():
    rm = _lenient_risk_manager()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=140.0, entry_time=now - timedelta(minutes=5), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=120.0, min_progress_pnl_pct=0.05, progress_check_minutes=30.0,
        eod_close_enabled=False,
    )
    assert reason == "scalping_take_profit"


def test_evaluate_scalping_exit_horizon_expired():
    rm = _lenient_risk_manager()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=101.0, entry_time=now - timedelta(minutes=121), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=120.0, min_progress_pnl_pct=0.05, progress_check_minutes=200.0,
        eod_close_enabled=False,
    )
    assert reason == "scalping_horizon_expired"


def test_evaluate_scalping_exit_no_progress():
    rm = _lenient_risk_manager()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=101.0, entry_time=now - timedelta(minutes=31), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=120.0, min_progress_pnl_pct=0.05, progress_check_minutes=30.0,
        eod_close_enabled=False,
    )
    assert reason == "scalping_no_progress"


def test_evaluate_scalping_exit_no_progress_not_triggered_before_check_window():
    rm = _lenient_risk_manager()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=101.0, entry_time=now - timedelta(minutes=10), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=120.0, min_progress_pnl_pct=0.05, progress_check_minutes=30.0,
        eod_close_enabled=False,
    )
    assert reason is None


def test_evaluate_scalping_exit_eod_close_triggers_past_close_time():
    rm = _lenient_risk_manager()
    # 20:00 UTC = 17:00 ART (UTC-3), ya paso el cierre 16:50 ART configurado.
    now = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=101.0, entry_time=now - timedelta(minutes=5), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=99999.0, min_progress_pnl_pct=0.0, progress_check_minutes=99999.0,
        eod_close_enabled=True, eod_close_time="16:50", eod_timezone_offset_hours=-3.0,
    )
    assert reason == "scalping_eod_close"


def test_evaluate_scalping_exit_eod_close_not_yet_before_close_time():
    rm = _lenient_risk_manager()
    # 18:00 UTC = 15:00 ART, todavia no llego a las 16:50 ART.
    now = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=101.0, entry_time=now - timedelta(minutes=5), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=99999.0, min_progress_pnl_pct=0.0, progress_check_minutes=99999.0,
        eod_close_enabled=True, eod_close_time="16:50", eod_timezone_offset_hours=-3.0,
    )
    assert reason is None


def test_evaluate_scalping_exit_eod_close_fires_even_without_current_price():
    rm = _lenient_risk_manager()
    now = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=None, entry_time=now - timedelta(minutes=5), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=99999.0, min_progress_pnl_pct=0.0, progress_check_minutes=99999.0,
        eod_close_enabled=True, eod_close_time="16:50", eod_timezone_offset_hours=-3.0,
    )
    assert reason == "scalping_eod_close"


def test_evaluate_scalping_exit_invalid_eod_time_format_never_crashes_or_forces_close():
    rm = _lenient_risk_manager()
    now = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=101.0, entry_time=now - timedelta(minutes=5), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=99999.0, min_progress_pnl_pct=0.0, progress_check_minutes=99999.0,
        eod_close_enabled=True, eod_close_time="not-a-time", eod_timezone_offset_hours=-3.0,
    )
    assert reason is None


def test_evaluate_scalping_exit_stop_loss_takes_priority_over_eod():
    rm = _lenient_risk_manager()
    now = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)  # ya paso el cierre EOD
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=70.0, entry_time=now - timedelta(minutes=5), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=99999.0, min_progress_pnl_pct=0.0, progress_check_minutes=99999.0,
        eod_close_enabled=True, eod_close_time="16:50", eod_timezone_offset_hours=-3.0,
    )
    assert reason == "scalping_stop_loss"


def test_evaluate_scalping_exit_none_when_nothing_triggers():
    rm = _lenient_risk_manager()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)  # 09:00 ART, lejos del cierre
    reason = rm.evaluate_scalping_exit(
        entry_price=100.0, current_price=103.0, entry_time=now - timedelta(minutes=5), now=now,
        expiry=date(2026, 9, 4), stop_loss_pct=0.25, take_profit_pct=0.35,
        max_holding_minutes=120.0, min_progress_pnl_pct=0.05, progress_check_minutes=30.0,
        eod_close_enabled=True, eod_close_time="16:50", eod_timezone_offset_hours=-3.0,
    )
    assert reason is None


# ---------------------------------------------------------------------------
# strategy/scalping.py:ScalpingStrategy.scan_entry_signals
# ---------------------------------------------------------------------------

def test_scan_entry_signals_ask_depth_filter_blocks_thin_book():
    cfg = _scalping_config(min_ask_size_for_entry=200.0)  # muy por encima del ask_size=50.0 de _cheap_calls_surface
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=cfg)
    surface = _cheap_calls_surface()
    order_books = {q.symbol: q.book for q in surface.quotes}
    signals = strategy.scan_entry_signals(surface, {}, order_books, trend="BULLISH")
    assert signals == []


def test_scan_entry_signals_ask_depth_filter_allows_deep_book():
    cfg = _scalping_config(min_ask_size_for_entry=10.0)
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=cfg)
    surface = _cheap_calls_surface()
    order_books = {q.symbol: q.book for q in surface.quotes}
    signals = strategy.scan_entry_signals(surface, {}, order_books, trend="BULLISH")
    assert len(signals) > 0
    assert all(s.option_type is OptionType.CALL for s in signals)


def test_scan_entry_signals_ask_depth_filter_disabled_ignores_depth():
    cfg = _scalping_config(enable_min_ask_depth_filter=False, min_ask_size_for_entry=99999.0)
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=cfg)
    surface = _cheap_calls_surface()
    order_books = {q.symbol: q.book for q in surface.quotes}
    signals = strategy.scan_entry_signals(surface, {}, order_books, trend="BULLISH")
    assert len(signals) > 0


def test_scan_entry_signals_missing_order_book_is_treated_as_failing_depth():
    cfg = _scalping_config(min_ask_size_for_entry=10.0)
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=cfg)
    surface = _cheap_calls_surface()
    signals = strategy.scan_entry_signals(surface, {}, {}, trend="BULLISH")  # order_books vacio
    assert signals == []


def test_scan_entry_signals_bearish_trend_only_considers_puts():
    cfg = _scalping_config(min_ask_size_for_entry=10.0)
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=cfg)
    surface = _cheap_calls_surface()  # todas CALL
    order_books = {q.symbol: q.book for q in surface.quotes}
    signals = strategy.scan_entry_signals(surface, {}, order_books, trend="BEARISH")
    assert signals == []  # bloqueo direccional: BEARISH descarta calls


def test_scan_entry_signals_diagnostics_exposed_from_base_scanner():
    cfg = _scalping_config(min_ask_size_for_entry=10.0)
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=cfg)
    surface = _cheap_calls_surface()
    order_books = {q.symbol: q.book for q in surface.quotes}
    strategy.scan_entry_signals(surface, {}, order_books, trend="BULLISH")
    assert strategy.last_scan_diagnostics is not None
    assert strategy.last_scan_diagnostics.qualified > 0


def test_scan_entry_signals_feeds_iv_tracker_even_for_filtered_quotes():
    cfg = _scalping_config(min_ask_size_for_entry=99999.0)  # bloquea TODO por profundidad
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=cfg)
    surface = _cheap_calls_surface()
    order_books = {q.symbol: q.book for q in surface.quotes}
    signals = strategy.scan_entry_signals(surface, {}, order_books, trend="BULLISH")
    assert signals == []
    # el tracker de IV se alimenta de TODAS las cotizaciones del scan,
    # independientemente del filtro de profundidad de ASK.
    assert strategy.iv_tracker.sample_count("GFGC6600O") == 1


# ---------------------------------------------------------------------------
# strategy/scalping.py:ScalpingStrategy.build_exit_signals + AISLAMIENTO
# ---------------------------------------------------------------------------

def _open_position(symbol, entry_price, entry_time, strategy_tag, expiry=date(2026, 9, 4)):
    return Position(
        symbol=symbol, quantity=10, multiplier=100, greeks_per_unit={"delta": 0.5, "vega": 1.0},
        expiry=expiry, entry_price=entry_price, entry_time=entry_time, strategy_tag=strategy_tag,
    )


def test_build_exit_signals_only_touches_scalping_tagged_positions():
    cfg = _scalping_config()
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=cfg)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    portfolio = Portfolio()
    portfolio.add(_open_position("SCALP1", 100.0, now - timedelta(minutes=5), strategy_tag="scalping"))
    portfolio.add(_open_position("WEEKLY1", 100.0, now - timedelta(minutes=5), strategy_tag="weekly_asymmetric"))
    portfolio.add(_open_position("UNTAGGED1", 100.0, now - timedelta(minutes=5), strategy_tag=None))

    # precio muy por debajo de entry_price -> stop loss para CUALQUIERA de
    # las tres bases si se evaluara, pero solo debe procesarse SCALP1.
    current_prices = {"SCALP1": 50.0, "WEEKLY1": 50.0, "UNTAGGED1": 50.0}
    signals = strategy.build_exit_signals(portfolio, current_prices, now)
    symbols_touched = {s.symbol for s in signals}
    assert symbols_touched == {"SCALP1"}


def test_weekly_asymmetric_build_exit_signals_ignores_scalping_tagged_positions():
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager())
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    portfolio = Portfolio()
    portfolio.add(_open_position("SCALP1", 100.0, now - timedelta(days=10), strategy_tag="scalping"))
    portfolio.add(_open_position("OCTUBRE", 100.0, now - timedelta(days=10), strategy_tag=None))

    current_prices = {"SCALP1": 20.0, "OCTUBRE": 20.0}  # -80%, dispara stop_loss default (50%) para cualquiera
    signals = strategy.build_exit_signals(portfolio, current_prices, now)
    symbols_touched = {s.symbol for s in signals}
    # Solo la posicion SIN marca (equivalente a "weekly_asymmetric", como la
    # de Octubre en produccion) se procesa - la de scalping queda intacta.
    assert symbols_touched == {"OCTUBRE"}


def test_build_exit_signals_iv_mean_reversion_exit():
    cfg = _scalping_config(stop_loss_pct=0.99, take_profit_pct=0.99, max_holding_minutes=99999.0,
                            min_progress_pnl_pct=0.0, progress_check_minutes=99999.0, eod_close_enabled=False)
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=cfg)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    # Historia de dislocaciones con algo de ruido real (desvio > 0) cuya
    # ULTIMA muestra ya volvio a estar cerca del promedio reciente (z~0) -
    # con una serie perfectamente constante el desvio estandar da 0 y el
    # z-score queda indefinido (ver IVMeanReversionTracker.zscore), por eso
    # se agrega variacion minima alrededor de -3.0.
    baseline = [-3.0, -2.8, -3.2, -2.9, -3.1, -3.0, -2.85, -3.15, -2.95, -3.0]
    for i, v in enumerate(baseline):
        strategy.iv_tracker.update("SCALP1", v, now=now - timedelta(seconds=(len(baseline) - i)))

    portfolio = Portfolio()
    portfolio.add(_open_position("SCALP1", 100.0, now - timedelta(minutes=5), strategy_tag="scalping"))
    signals = strategy.build_exit_signals(portfolio, {"SCALP1": 101.0}, now)
    assert len(signals) == 1
    assert signals[0].reason == "scalping_iv_mean_reversion"


def test_build_exit_signals_skips_positions_missing_entry_metadata():
    strategy = ScalpingStrategy(_lenient_risk_manager(), config=_scalping_config())
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    portfolio = Portfolio()
    portfolio.add(Position(symbol="SCALP1", quantity=10, multiplier=100, strategy_tag="scalping"))
    signals = strategy.build_exit_signals(portfolio, {"SCALP1": 50.0}, now)
    assert signals == []


# ---------------------------------------------------------------------------
# Capital separado por estrategia (ver run_bot.py:_capital_available_ars) -
# smoke test directo del dataclass Position/strategy_tag sin levantar
# GgalOptionsBot completo (eso se cubre abajo, integracion).
# ---------------------------------------------------------------------------

def test_position_default_strategy_tag_is_none_backward_compatible():
    pos = Position(symbol="X", quantity=1, multiplier=100)
    assert pos.strategy_tag is None


# ---------------------------------------------------------------------------
# Integracion: run_bot.py:GgalOptionsBot - el modulo es aditivo/opt-in
# ---------------------------------------------------------------------------

def test_bot_scalping_disabled_by_default_wires_nothing():
    original_enabled = SETTINGS.scalping.enabled
    SETTINGS.scalping.enabled = False
    try:
        from run_bot import GgalOptionsBot
        bot = GgalOptionsBot()
        assert bot.scalping_enabled is False
        assert bot.scalping_strategy is None
        assert bot.scalping_position_sizer is None
        assert bot.intraday_engine is None
    finally:
        SETTINGS.scalping.enabled = original_enabled


def test_bot_scalping_enabled_wires_independent_strategy_and_sizer():
    original_enabled = SETTINGS.scalping.enabled
    SETTINGS.scalping.enabled = True
    try:
        from run_bot import GgalOptionsBot
        bot = GgalOptionsBot()
        assert bot.scalping_enabled is True
        assert bot.scalping_strategy is not None
        assert bot.scalping_position_sizer is not None
        assert bot.intraday_engine is not None
        # El sizer de scalping es una instancia DISTINTA del de la
        # estrategia principal, con su propio capital configurado.
        assert bot.scalping_position_sizer is not bot.position_sizer
    finally:
        SETTINGS.scalping.enabled = original_enabled


def test_bot_capital_available_ars_pools_are_independent():
    original_enabled = SETTINGS.scalping.enabled
    SETTINGS.scalping.enabled = True
    try:
        from run_bot import GgalOptionsBot
        bot = GgalOptionsBot()
        bot.portfolio.add(_open_position(
            "WEEKLY1", entry_price=1000.0, entry_time=datetime.now(timezone.utc), strategy_tag="weekly_asymmetric",
        ))
        weekly_capital_before = SETTINGS.long_first.max_capital_ars
        scalping_capital_before = SETTINGS.scalping.max_capital_ars

        weekly_available = bot._capital_available_ars("weekly_asymmetric")
        scalping_available = bot._capital_available_ars("scalping")

        committed = 10 * 1000.0 * 100  # quantity * entry_price * multiplier
        assert weekly_available == max(0.0, weekly_capital_before - committed)
        # La posicion de weekly_asymmetric NO debe reducir el pool de scalping.
        assert scalping_available == scalping_capital_before
    finally:
        SETTINGS.scalping.enabled = original_enabled


# ---------------------------------------------------------------------------
# AISLAMIENTO del techo de riesgo de Griegas (CORRECCION 2026-09-03, ver
# comentario largo junto a ScalpingConfig.max_vega_total/max_gamma_total en
# config.py y README "Interaccion con el techo de Griegas"): antes de esta
# correccion, scalping y weekly_asymmetric/vol_arbitrage compartian el
# MISMO RiskManager, evaluado contra self.portfolio.total_greeks() (la
# cuenta ENTERA) - un book de una estrategia que ya excedia el techo
# bloqueaba tambien las entradas de la OTRA, aunque esta ultima no tuviera
# ninguna posicion propia abierta (confirmado en produccion: vega=9550.13
# > RiskConfig.max_vega_total=5000.0 bloqueando entradas de ambas
# estrategias por igual). Estos tests prueban que, con
# Portfolio.greeks_for_strategy_tag() + self.scalping_risk_manager +
# _act_on_entry_signal(risk_manager=...), cada estrategia queda sujeta
# UNICAMENTE a su propio book y su propio techo.
# ---------------------------------------------------------------------------

def test_portfolio_greeks_for_strategy_tag_isolates_vega_by_tag():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    portfolio = Portfolio()
    # vega por posicion = quantity * multiplier * greeks_per_unit["vega"]
    portfolio.add(Position(
        symbol="SCALP1", quantity=10, multiplier=100, entry_price=100.0, entry_time=now,
        greeks_per_unit={"vega": 1.0}, strategy_tag="scalping",
    ))
    portfolio.add(Position(
        symbol="WEEKLY1", quantity=50, multiplier=100, entry_price=100.0, entry_time=now,
        greeks_per_unit={"vega": 2.0}, strategy_tag="weekly_asymmetric",
    ))
    portfolio.add(Position(
        symbol="UNTAGGED1", quantity=5, multiplier=100, entry_price=100.0, entry_time=now,
        greeks_per_unit={"vega": 3.0}, strategy_tag=None,
    ))

    scalping_totals = portfolio.greeks_for_strategy_tag("scalping")
    weekly_totals = portfolio.greeks_for_strategy_tag("weekly_asymmetric")

    assert scalping_totals["vega"] == 10 * 100 * 1.0
    # UNTAGGED1 (strategy_tag=None) cuenta como "weekly_asymmetric" por
    # convencion (ver Position.strategy_tag) - debe sumarse junto a WEEKLY1.
    assert weekly_totals["vega"] == (50 * 100 * 2.0) + (5 * 100 * 3.0)
    # La suma total de ambos pools (sin superposicion) debe coincidir con
    # total_greeks() sobre toda la cartera.
    assert scalping_totals["vega"] + weekly_totals["vega"] == portfolio.total_greeks()["vega"]


def test_bot_scalping_has_own_risk_manager_isolated_from_primary():
    original_enabled = SETTINGS.scalping.enabled
    SETTINGS.scalping.enabled = True
    try:
        bot = GgalOptionsBot()
        assert bot.scalping_risk_manager is not None
        assert bot.scalping_risk_manager is not bot.risk_manager
        assert bot.scalping_strategy.risk_manager is bot.scalping_risk_manager
    finally:
        SETTINGS.scalping.enabled = original_enabled


def test_bot_act_on_entry_signal_scalping_not_blocked_by_weekly_asymmetric_vega_breach():
    original_enabled = SETTINGS.scalping.enabled
    original_shadow = SETTINGS.shadow.enabled
    SETTINGS.scalping.enabled = True
    # SETTINGS.shadow.enabled=True: fill sincronico (ver order_gateway.py.send),
    # necesario para que _act_on_entry_signal() agregue la Position en el
    # mismo llamado (ver test_execution_pipeline.py, mismo patron).
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()

        # Libro de weekly_asymmetric bien por encima del techo COMPARTIDO de
        # RiskConfig.max_vega_total (5000.0 default): 100 * 100 * 1.0 = 10000.
        bot.portfolio.add(Position(
            symbol="WEEKLY_HEAVY", quantity=100, multiplier=100,
            entry_price=100.0, entry_time=datetime.now(timezone.utc) - timedelta(days=1),
            greeks_per_unit={"delta": 0.5, "gamma": 0.01, "vega": 1.0, "theta": -1.0},
            expiry=date(2026, 9, 4), strategy_tag="weekly_asymmetric",
        ))
        # Confirma la premisa: la cuenta ENTERA (vista compartida) esta en
        # breach - si _act_on_entry_signal siguiera usando
        # self.portfolio.total_greeks()/self.risk_manager, la entrada de
        # abajo NUNCA se abriria.
        assert bot.risk_manager.should_halt_new_positions(bot.portfolio.total_greeks())

        new_quote = _quote("GFSCALP1", 6600.0, 0.30, 6600.0, 1, bid=95.0, ask=105.0,
                            greeks={"delta": 0.5, "gamma": 0.01, "vega": 1.0, "theta": -1.0})
        bot.option_chain.upsert_quote(new_quote)
        signal = EntrySignal(
            symbol="GFSCALP1", option_type=OptionType.CALL, premium_reference=new_quote.book.mid,
        )
        bot._act_on_entry_signal(
            signal, spot=6600.0, strategy_tag="scalping",
            position_sizer=bot.scalping_position_sizer, risk_manager=bot.scalping_risk_manager,
        )

        assert bot._position_quantity("GFSCALP1") > 0
    finally:
        SETTINGS.scalping.enabled = original_enabled
        SETTINGS.shadow.enabled = original_shadow


def test_bot_act_on_entry_signal_weekly_asymmetric_not_blocked_by_scalping_vega_breach():
    original_enabled = SETTINGS.scalping.enabled
    original_shadow = SETTINGS.shadow.enabled
    SETTINGS.scalping.enabled = True
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()

        # Libro de scalping bien por encima de SU PROPIO techo
        # (ScalpingConfig.max_vega_total, default 3000.0): 50 * 100 * 1.0 = 5000.
        bot.portfolio.add(Position(
            symbol="SCALP_HEAVY", quantity=50, multiplier=100,
            entry_price=100.0, entry_time=datetime.now(timezone.utc) - timedelta(minutes=5),
            greeks_per_unit={"delta": 0.5, "gamma": 0.01, "vega": 1.0, "theta": -1.0},
            expiry=date(2026, 9, 4), strategy_tag="scalping",
        ))
        assert bot.scalping_risk_manager.should_halt_new_positions(
            bot.portfolio.greeks_for_strategy_tag("scalping")
        )

        new_quote = _quote("GFWEEKLY1", 6600.0, 0.30, 6600.0, 5, bid=95.0, ask=105.0,
                            greeks={"delta": 0.5, "gamma": 0.01, "vega": 1.0, "theta": -1.0})
        bot.option_chain.upsert_quote(new_quote)
        signal = EntrySignal(
            symbol="GFWEEKLY1", option_type=OptionType.CALL, premium_reference=new_quote.book.mid,
        )
        # Sin overrides: usa self.risk_manager/self.position_sizer (default
        # de weekly_asymmetric), que NO deberian verse afectados por el
        # book de scalping de arriba.
        bot._act_on_entry_signal(signal, spot=6600.0, strategy_tag="weekly_asymmetric")

        assert bot._position_quantity("GFWEEKLY1") > 0
    finally:
        SETTINGS.scalping.enabled = original_enabled
        SETTINGS.shadow.enabled = original_shadow


def test_bot_act_on_entry_signal_scalping_still_blocked_by_its_own_vega_breach():
    original_enabled = SETTINGS.scalping.enabled
    SETTINGS.scalping.enabled = True
    try:
        bot = GgalOptionsBot()

        # Libro de scalping bien por encima de SU PROPIO techo (50 * 100 *
        # 1.0 = 5000 > ScalpingConfig.max_vega_total=3000.0 default) - esta
        # vez la entrada NUEVA tambien es de scalping, asi que SI debe
        # bloquearse: el aislamiento no debe convertirse en "scalping nunca
        # se frena".
        bot.portfolio.add(Position(
            symbol="SCALP_HEAVY", quantity=50, multiplier=100,
            entry_price=100.0, entry_time=datetime.now(timezone.utc) - timedelta(minutes=5),
            greeks_per_unit={"delta": 0.5, "gamma": 0.01, "vega": 1.0, "theta": -1.0},
            expiry=date(2026, 9, 4), strategy_tag="scalping",
        ))

        new_quote = _quote("GFSCALP2", 6600.0, 0.30, 6600.0, 1, bid=95.0, ask=105.0,
                            greeks={"delta": 0.5, "gamma": 0.01, "vega": 1.0, "theta": -1.0})
        bot.option_chain.upsert_quote(new_quote)
        signal = EntrySignal(
            symbol="GFSCALP2", option_type=OptionType.CALL, premium_reference=new_quote.book.mid,
        )
        bot._act_on_entry_signal(
            signal, spot=6600.0, strategy_tag="scalping",
            position_sizer=bot.scalping_position_sizer, risk_manager=bot.scalping_risk_manager,
        )

        assert bot._position_quantity("GFSCALP2") == 0
    finally:
        SETTINGS.scalping.enabled = original_enabled


ALL_TESTS = [
    test_passes_min_ask_depth_blocks_below_floor,
    test_passes_min_ask_depth_allows_at_or_above_floor,
    test_passes_min_ask_depth_blocks_when_no_ask_side_at_all,
    test_iv_tracker_zscore_none_without_enough_samples,
    test_iv_tracker_detects_extreme_deviation_then_reversion,
    test_iv_tracker_ignores_none_dislocation,
    test_iv_tracker_trims_window_by_max_age,
    test_intraday_bar_aggregator_builds_ohlc_within_same_bucket,
    test_intraday_bar_aggregator_closes_bucket_on_next_interval,
    test_intraday_bar_aggregator_ignores_invalid_price,
    test_intraday_bar_aggregator_respects_max_bars_retained,
    test_multi_timeframe_engine_combined_trend_neutral_without_enough_bars,
    test_multi_timeframe_engine_refresh_cache_respects_interval,
    test_multi_timeframe_engine_disagreement_forces_neutral,
    test_evaluate_scalping_exit_stop_loss,
    test_evaluate_scalping_exit_take_profit,
    test_evaluate_scalping_exit_horizon_expired,
    test_evaluate_scalping_exit_no_progress,
    test_evaluate_scalping_exit_no_progress_not_triggered_before_check_window,
    test_evaluate_scalping_exit_eod_close_triggers_past_close_time,
    test_evaluate_scalping_exit_eod_close_not_yet_before_close_time,
    test_evaluate_scalping_exit_eod_close_fires_even_without_current_price,
    test_evaluate_scalping_exit_invalid_eod_time_format_never_crashes_or_forces_close,
    test_evaluate_scalping_exit_stop_loss_takes_priority_over_eod,
    test_evaluate_scalping_exit_none_when_nothing_triggers,
    test_scan_entry_signals_ask_depth_filter_blocks_thin_book,
    test_scan_entry_signals_ask_depth_filter_allows_deep_book,
    test_scan_entry_signals_ask_depth_filter_disabled_ignores_depth,
    test_scan_entry_signals_missing_order_book_is_treated_as_failing_depth,
    test_scan_entry_signals_bearish_trend_only_considers_puts,
    test_scan_entry_signals_diagnostics_exposed_from_base_scanner,
    test_scan_entry_signals_feeds_iv_tracker_even_for_filtered_quotes,
    test_build_exit_signals_only_touches_scalping_tagged_positions,
    test_weekly_asymmetric_build_exit_signals_ignores_scalping_tagged_positions,
    test_build_exit_signals_iv_mean_reversion_exit,
    test_build_exit_signals_skips_positions_missing_entry_metadata,
    test_position_default_strategy_tag_is_none_backward_compatible,
    test_bot_scalping_disabled_by_default_wires_nothing,
    test_bot_scalping_enabled_wires_independent_strategy_and_sizer,
    test_bot_capital_available_ars_pools_are_independent,
    test_portfolio_greeks_for_strategy_tag_isolates_vega_by_tag,
    test_bot_scalping_has_own_risk_manager_isolated_from_primary,
    test_bot_act_on_entry_signal_scalping_not_blocked_by_weekly_asymmetric_vega_breach,
    test_bot_act_on_entry_signal_weekly_asymmetric_not_blocked_by_scalping_vega_breach,
    test_bot_act_on_entry_signal_scalping_still_blocked_by_its_own_vega_breach,
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
