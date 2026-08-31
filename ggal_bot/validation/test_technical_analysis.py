"""
test_technical_analysis.py
============================
Suite de validacion del modulo data/technical_analysis.py: indicadores puros
(EMA/RSI/MACD/ADX), clasificacion de tendencia (compute_technical_snapshot /
get_daily_trend_signal), resolucion de fuente de velas (_resolve_bars_source,
patron auto/data912/synthetic) y el motor con cache (TechnicalAnalysisEngine).

Igual que el resto de validation/, corre sin pytest: `python -m
ggal_bot.validation.test_technical_analysis`. Todos los tests son
deterministicos (series sinteticas armadas a mano, no random sin seed) salvo
donde se indica explicitamente lo contrario.

NOTA (ver README / config.TechnicalAnalysisConfig): las series de tendencia
BAJISTA usadas aca son de caida ACELERADA (drift creciente en magnitud), no
de tasa constante. Esto no es un capricho del test: un MACD de precio crudo
(no logaritmico) aplicado sobre una caida sostenida a tasa CONSTANTE tiende a
un histograma POSITIVO en regimen estacionario (la EMA de una exponencial
decreciente es proporcional al precio, asi que la linea MACD converge
monotonicamente a cero desde abajo de su propia señal). Es una propiedad
matematica real del MACD estandar sobre precio crudo en tendencias largas y
de magnitud grande - no un bug de esta implementacion. Por eso BEARISH exige,
en la practica, una caida con momentum CRECIENTE (aceleracion), mientras que
BULLISH se satisface con una suba sostenida a tasa constante. Ver el
disclosure completo en README.md.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from ggal_bot.config import TechnicalAnalysisConfig
from ggal_bot.data import technical_analysis as ta
from ggal_bot.data.technical_analysis import (
    DailyBar,
    Data912DailyBarsSource,
    SyntheticDailyBarsSource,
    TechnicalAnalysisEngine,
    Trend,
    _resolve_bars_source,
    adx,
    compute_technical_snapshot,
    ema,
    get_daily_trend_signal,
    macd,
    rsi,
)


def _test_cfg(**overrides) -> TechnicalAnalysisConfig:
    """Config aislada de env vars, con overrides puntuales por test."""
    cfg = TechnicalAnalysisConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _make_bars(n: int, daily_return_fn, start_price: float = 5000.0, start_date=date(2026, 1, 2)):
    """
    Genera `n` velas diarias (dias habiles, lunes-viernes) deterministicas:
    `daily_return_fn(i)` da el retorno logaritmico-simple del dia `i` (0-based).
    High/low con un margen fijo minusculo sobre open/close (para que TR/+-DM
    de ADX no sean cero, sin inyectar ruido aleatorio).
    """
    bars = []
    d = start_date
    price = start_price
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        prev = price
        price = prev * math.exp(daily_return_fn(i))
        high = max(prev, price) * 1.0015
        low = min(prev, price) * 0.9985
        bars.append(DailyBar(bar_date=d, open=prev, high=high, low=low, close=price, volume=500_000.0))
        d += timedelta(days=1)
    return bars


# ---------------------------------------------------------------------------
# Indicadores puros
# ---------------------------------------------------------------------------

def test_ema_matches_manual_calculation():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ema(values, period=3)
    assert result[0] is None and result[1] is None
    assert result[2] == 2.0  # semilla = SMA(1,2,3)
    assert abs(result[3] - 3.0) < 1e-9  # k=0.5: 4*0.5 + 2*0.5
    assert abs(result[4] - 4.0) < 1e-9  # 5*0.5 + 3*0.5


def test_ema_insufficient_data_returns_all_none():
    result = ema([1.0, 2.0], period=5)
    assert result == [None, None]


def test_rsi_all_gains_saturates_at_100():
    closes = [100.0 + i for i in range(20)]  # monotonico creciente, sin perdidas
    result = rsi(closes, period=14)
    assert result[-1] == 100.0


def test_rsi_all_losses_saturates_at_0():
    closes = [200.0 - i for i in range(20)]  # monotonico decreciente, sin ganancias
    result = rsi(closes, period=14)
    assert result[-1] == 0.0


def test_macd_returns_three_lists_aligned_with_input():
    closes = [5000.0 * math.exp(0.001 * i) for i in range(80)]
    macd_line, signal_line, histogram = macd(closes, fast_period=12, slow_period=26, signal_period=9)
    assert len(macd_line) == len(signal_line) == len(histogram) == len(closes)
    assert histogram[-1] is not None  # con 80 barras ya hay historia de sobra (26+9)


def test_adx_requires_minimum_bars_before_first_value():
    n = 10  # muy por debajo de period+1=15
    highs = [100.0 + i for i in range(n)]
    lows = [99.0 + i for i in range(n)]
    closes = [99.5 + i for i in range(n)]
    adx_out, plus_di, minus_di = adx(highs, lows, closes, period=14)
    assert all(v is None for v in adx_out)


def test_adx_uptrend_produces_high_plus_di_dominance():
    bars = _make_bars(60, lambda i: 0.01)  # suba sostenida y limpia
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    adx_out, plus_di, minus_di = adx(highs, lows, closes, period=14)
    assert adx_out[-1] is not None
    assert plus_di[-1] > minus_di[-1]


# ---------------------------------------------------------------------------
# Clasificacion de tendencia (compute_technical_snapshot / get_daily_trend_signal)
# ---------------------------------------------------------------------------

def test_compute_technical_snapshot_insufficient_bars_is_neutral_with_reason():
    cfg = _test_cfg(min_bars_required=60)
    bars = _make_bars(30, lambda i: 0.002)  # menos que min_bars_required
    snapshot = compute_technical_snapshot(bars, cfg, data_source="test")
    assert snapshot.trend is Trend.NEUTRAL
    assert "insuficientes" in snapshot.reason.lower()
    assert snapshot.bars_used == 30


def test_compute_technical_snapshot_sustained_uptrend_is_bullish():
    cfg = _test_cfg()
    bars = _make_bars(150, lambda i: 0.003)  # suba sostenida a tasa constante
    snapshot = compute_technical_snapshot(bars, cfg, data_source="test")
    assert snapshot.trend is Trend.BULLISH
    assert snapshot.last_close > snapshot.ema_fast > snapshot.ema_slow
    assert snapshot.adx_value > cfg.adx_trend_threshold
    assert snapshot.macd_histogram > 0


def test_compute_technical_snapshot_accelerating_downtrend_is_bearish():
    # Caida con momentum CRECIENTE (ver nota de modulo): esto es lo que
    # efectivamente hace falta para un histograma MACD negativo en una
    # tendencia bajista de precio crudo sostenida en el tiempo.
    cfg = _test_cfg()
    bars = _make_bars(150, lambda i: -0.0005 - 0.00002 * i)
    snapshot = compute_technical_snapshot(bars, cfg, data_source="test")
    assert snapshot.trend is Trend.BEARISH
    assert snapshot.last_close < snapshot.ema_fast < snapshot.ema_slow
    assert snapshot.adx_value > cfg.adx_trend_threshold
    assert snapshot.macd_histogram < 0


def test_compute_technical_snapshot_constant_rate_decline_is_not_bearish():
    """
    Documenta explicitamente la propiedad matematica del modulo (ver
    docstring de este archivo): una caida sostenida a tasa CONSTANTE (sin
    aceleracion) NO clasifica BEARISH bajo MACD de precio crudo, porque el
    histograma tiende a positivo en regimen estacionario. No es un bug -
    verifica que el codigo se comporta exactamente como la matematica predice,
    para que una futura reescritura no lo "corrija" sin querer sin darse
    cuenta de la implicancia (o que lo haga a sabiendas, si se decide migrar
    a un MACD sobre log-precio).
    """
    cfg = _test_cfg()
    bars = _make_bars(150, lambda i: -0.003)  # tasa constante, sin aceleracion
    snapshot = compute_technical_snapshot(bars, cfg, data_source="test")
    assert snapshot.trend is not Trend.BEARISH
    assert snapshot.macd_histogram > 0  # el nucleo contraintuitivo del hallazgo


def test_compute_technical_snapshot_whipsaw_is_neutral():
    bars = _make_bars(150, lambda i: 0.006 if i % 2 == 0 else -0.006)  # zigzag simetrico, sin tendencia neta
    cfg = _test_cfg()
    snapshot = compute_technical_snapshot(bars, cfg, data_source="test")
    assert snapshot.trend is Trend.NEUTRAL


def test_get_daily_trend_signal_returns_plain_string_literal():
    cfg = _test_cfg()
    bars = _make_bars(150, lambda i: 0.003)
    result = get_daily_trend_signal(bars, cfg)
    assert result == "BULLISH"
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Momentum Shift / Early Reversal Override (ver data.technical_analysis.
# MomentumShift y config.TechnicalAnalysisConfig.enable_momentum_shift_override)
#
# NOTA DE METODO: en vez de buscar una combinacion de precios que produzca un
# RSI(14) exacto (posible, pero fragil y opaco - ver la exploracion numerica
# descartada en el historial de este archivo), estas pruebas monkeypatchean
# `ta.rsi()` (mismo patron ya usado en
# test_resolve_bars_source_auto_falls_back_to_synthetic_when_real_source_fails
# para Data912DailyBarsSource.fetch) para controlar exactamente la relacion
# rsi_series[-1] vs rsi_series[-1-lookback], que es lo unico que le importa a
# la logica de Momentum Shift - la trayectoria de precio en si ya esta
# cubierta por los tests de clasificacion BULLISH/BEARISH/NEUTRAL de arriba.
# Se restaura siempre ta.rsi en el finally.
# ---------------------------------------------------------------------------

def _bars_bullish_150():
    return _make_bars(150, lambda i: 0.003)  # mismo patron que test_..._sustained_uptrend_is_bullish


def _bars_bearish_150():
    return _make_bars(150, lambda i: -0.0005 - 0.00002 * i)  # mismo patron que ..._accelerating_downtrend_is_bearish


def test_momentum_shift_early_bearish_reversal_detected_under_bullish_trend():
    original_rsi = ta.rsi
    lookback = 3

    def _fake_rsi(closes, period=14):  # noqa: ARG001
        series = [50.0] * len(closes)
        series[-1 - lookback] = 70.0
        series[-1] = 40.0  # delta = -30, bien por debajo de -momentum_shift_rsi_delta (8.0)
        return series

    ta.rsi = _fake_rsi
    try:
        cfg = _test_cfg(momentum_shift_lookback_bars=lookback, momentum_shift_rsi_delta=8.0)
        snapshot = compute_technical_snapshot(_bars_bullish_150(), cfg, data_source="test")
        assert snapshot.trend is Trend.BULLISH
        assert snapshot.momentum_shift == ta.MomentumShift.EARLY_BEARISH_REVERSAL.value
    finally:
        ta.rsi = original_rsi


def test_momentum_shift_early_bullish_reversal_detected_under_bearish_trend():
    original_rsi = ta.rsi
    lookback = 3

    def _fake_rsi(closes, period=14):  # noqa: ARG001
        series = [50.0] * len(closes)
        series[-1 - lookback] = 30.0
        series[-1] = 70.0  # delta = +40, bien por encima de momentum_shift_rsi_delta (8.0)
        return series

    ta.rsi = _fake_rsi
    try:
        cfg = _test_cfg(momentum_shift_lookback_bars=lookback, momentum_shift_rsi_delta=8.0)
        snapshot = compute_technical_snapshot(_bars_bearish_150(), cfg, data_source="test")
        assert snapshot.trend is Trend.BEARISH
        assert snapshot.momentum_shift == ta.MomentumShift.EARLY_BULLISH_REVERSAL.value
    finally:
        ta.rsi = original_rsi


def test_momentum_shift_not_detected_when_rsi_delta_below_threshold():
    original_rsi = ta.rsi
    lookback = 3

    def _fake_rsi(closes, period=14):  # noqa: ARG001
        series = [50.0] * len(closes)
        series[-1 - lookback] = 55.0
        series[-1] = 50.0  # delta = -5, por debajo del umbral de 8.0
        return series

    ta.rsi = _fake_rsi
    try:
        cfg = _test_cfg(momentum_shift_lookback_bars=lookback, momentum_shift_rsi_delta=8.0)
        snapshot = compute_technical_snapshot(_bars_bullish_150(), cfg, data_source="test")
        assert snapshot.trend is Trend.BULLISH
        assert snapshot.momentum_shift is None
    finally:
        ta.rsi = original_rsi


def test_momentum_shift_not_evaluated_under_neutral_trend():
    """Bajo NEUTRAL, bullish y bearish son ambos False: momentum_shift no se computa (queda None) sin importar el RSI."""
    original_rsi = ta.rsi
    lookback = 3

    def _fake_rsi(closes, period=14):  # noqa: ARG001
        series = [50.0] * len(closes)
        series[-1 - lookback] = 90.0
        series[-1] = 10.0  # delta enorme, pero irrelevante bajo NEUTRAL
        return series

    ta.rsi = _fake_rsi
    try:
        cfg = _test_cfg(momentum_shift_lookback_bars=lookback, momentum_shift_rsi_delta=8.0)
        bars = _make_bars(150, lambda i: 0.006 if i % 2 == 0 else -0.006)  # whipsaw simetrico -> NEUTRAL
        snapshot = compute_technical_snapshot(bars, cfg, data_source="test")
        assert snapshot.trend is Trend.NEUTRAL
        assert snapshot.momentum_shift is None
    finally:
        ta.rsi = original_rsi


def test_momentum_shift_override_toggle_disables_detection():
    original_rsi = ta.rsi
    lookback = 3

    def _fake_rsi(closes, period=14):  # noqa: ARG001
        series = [50.0] * len(closes)
        series[-1 - lookback] = 70.0
        series[-1] = 40.0  # mismo delta que si detecta test_momentum_shift_early_bearish_reversal_...
        return series

    ta.rsi = _fake_rsi
    try:
        cfg = _test_cfg(
            momentum_shift_lookback_bars=lookback, momentum_shift_rsi_delta=8.0,
            enable_momentum_shift_override=False,
        )
        snapshot = compute_technical_snapshot(_bars_bullish_150(), cfg, data_source="test")
        assert snapshot.trend is Trend.BULLISH
        assert snapshot.momentum_shift is None
    finally:
        ta.rsi = original_rsi


# ---------------------------------------------------------------------------
# Resolucion de fuente de datos (_resolve_bars_source: auto/data912/synthetic)
# ---------------------------------------------------------------------------

def test_resolve_bars_source_forced_synthetic_is_always_available():
    cfg = _test_cfg(data_source="synthetic", lookback_bars=80)
    bars, source_name = _resolve_bars_source(cfg)
    assert source_name == "synthetic"
    assert len(bars) == 80
    assert all(isinstance(b, DailyBar) for b in bars)


def test_resolve_bars_source_auto_falls_back_to_synthetic_when_real_source_fails():
    """
    Se fuerza el fallo de Data912DailyBarsSource.fetch (sin pegarle a la red
    real, para que el test sea rapido y deterministico) y se verifica que
    'auto' cae al generador sintetico, igual que LiveShadowFeed con el feed
    en tiempo real.
    """
    original_fetch = Data912DailyBarsSource.fetch

    def _boom(self, ticker, lookback_bars):  # noqa: ARG001
        raise RuntimeError("simulado: data912 no responde")

    Data912DailyBarsSource.fetch = _boom
    try:
        cfg = _test_cfg(data_source="auto", lookback_bars=70)
        bars, source_name = _resolve_bars_source(cfg)
        assert source_name == "synthetic"
        assert len(bars) == 70
    finally:
        Data912DailyBarsSource.fetch = original_fetch


def test_resolve_bars_source_forced_data912_does_not_fall_back():
    """Con data_source='data912' forzado, un fallo NO debe caer a sintetico (se devuelve lo que haya, vacio incluido)."""
    original_fetch = Data912DailyBarsSource.fetch

    def _empty(self, ticker, lookback_bars):  # noqa: ARG001
        return []

    Data912DailyBarsSource.fetch = _empty
    try:
        cfg = _test_cfg(data_source="data912", lookback_bars=70)
        bars, source_name = _resolve_bars_source(cfg)
        assert source_name == "data912"
        assert bars == []
    finally:
        Data912DailyBarsSource.fetch = original_fetch


def test_synthetic_bars_source_produces_business_days_only():
    src = SyntheticDailyBarsSource(initial_close=5000.0, daily_vol=0.01, drift_per_day=0.0, seed=42)
    bars = src.fetch("GGAL", lookback_bars=40)
    assert len(bars) == 40
    assert all(b.bar_date.weekday() < 5 for b in bars)
    assert bars == sorted(bars, key=lambda b: b.bar_date)


# ---------------------------------------------------------------------------
# TechnicalAnalysisEngine (fetch + cache + exposicion de la ultima lectura)
# ---------------------------------------------------------------------------

def test_engine_get_trend_before_any_refresh_is_neutral():
    cfg = _test_cfg(data_source="synthetic")
    engine = TechnicalAnalysisEngine(config=cfg)
    assert engine.get_daily_trend_signal() == "NEUTRAL"
    assert engine.last_snapshot() is None


def test_engine_refresh_caches_within_interval():
    cfg = _test_cfg(data_source="synthetic", refresh_interval_seconds=3600.0, lookback_bars=80)
    engine = TechnicalAnalysisEngine(config=cfg)
    t0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    snap1 = engine.refresh(now=t0)
    snap2 = engine.refresh(now=t0 + timedelta(minutes=30))  # dentro del cache de 1h
    assert snap2 is snap1  # misma lectura cacheada, no se recalculo
    assert engine.get_daily_trend_signal() == snap1.trend.value


def test_engine_refresh_recomputes_after_interval_elapses():
    cfg = _test_cfg(data_source="synthetic", refresh_interval_seconds=3600.0, lookback_bars=80)
    engine = TechnicalAnalysisEngine(config=cfg)
    t0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    snap1 = engine.refresh(now=t0)
    snap2 = engine.refresh(now=t0 + timedelta(hours=2))  # vencio el cache
    assert snap2 is not snap1


def test_engine_force_refresh_bypasses_cache():
    cfg = _test_cfg(data_source="synthetic", refresh_interval_seconds=3600.0, lookback_bars=80)
    engine = TechnicalAnalysisEngine(config=cfg)
    t0 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    snap1 = engine.refresh(now=t0)
    snap2 = engine.refresh(now=t0 + timedelta(minutes=1), force=True)
    assert snap2 is not snap1


ALL_TESTS = [
    test_ema_matches_manual_calculation,
    test_ema_insufficient_data_returns_all_none,
    test_rsi_all_gains_saturates_at_100,
    test_rsi_all_losses_saturates_at_0,
    test_macd_returns_three_lists_aligned_with_input,
    test_adx_requires_minimum_bars_before_first_value,
    test_adx_uptrend_produces_high_plus_di_dominance,
    test_compute_technical_snapshot_insufficient_bars_is_neutral_with_reason,
    test_compute_technical_snapshot_sustained_uptrend_is_bullish,
    test_compute_technical_snapshot_accelerating_downtrend_is_bearish,
    test_compute_technical_snapshot_constant_rate_decline_is_not_bearish,
    test_compute_technical_snapshot_whipsaw_is_neutral,
    test_get_daily_trend_signal_returns_plain_string_literal,
    test_momentum_shift_early_bearish_reversal_detected_under_bullish_trend,
    test_momentum_shift_early_bullish_reversal_detected_under_bearish_trend,
    test_momentum_shift_not_detected_when_rsi_delta_below_threshold,
    test_momentum_shift_not_evaluated_under_neutral_trend,
    test_momentum_shift_override_toggle_disables_detection,
    test_resolve_bars_source_forced_synthetic_is_always_available,
    test_resolve_bars_source_auto_falls_back_to_synthetic_when_real_source_fails,
    test_resolve_bars_source_forced_data912_does_not_fall_back,
    test_synthetic_bars_source_produces_business_days_only,
    test_engine_get_trend_before_any_refresh_is_neutral,
    test_engine_refresh_caches_within_interval,
    test_engine_refresh_recomputes_after_interval_elapses,
    test_engine_force_refresh_bypasses_cache,
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
