"""
test_long_first_mode.py
==========================
Tests de sanity para el modo operativo "Long-First / Weekly Asymmetric":

    - risk/position_sizer.py        (sizing dinamico por capital asignado)
    - risk/risk_manager.py           (evaluate_position_exit: Stop Loss /
                                        Take Profit / horizonte semanal /
                                        guardia de fin de semana)
    - strategy/weekly_asymmetric.py  (solo señales de compra, filtro de
                                        horizonte/moneyness, spreads con la
                                        pata corta condicionada a una larga
                                        ya confirmada, glue de salidas)

Correr con:
    python -m ggal_bot.validation.test_long_first_mode
"""

from __future__ import annotations

import math
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timedelta, timezone

from ggal_bot.config import SETTINGS, LongFirstConfig
from ggal_bot.data.option_chain import OptionChain, OptionQuote, OrderBookSnapshot
from ggal_bot.data.technical_analysis import MomentumShift
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.portfolio.portfolio import Portfolio, Position
from ggal_bot.risk.position_sizer import PositionSizer
from ggal_bot.risk.risk_manager import RiskLimits, RiskManager
from ggal_bot.strategy.weekly_asymmetric import WeeklyAsymmetricStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lenient_risk_manager() -> RiskManager:
    """RiskManager con limites de liquidez laxos: los tests de entry-signal
    quieren aislar la logica de horizonte/moneyness/direccion, no la de
    liquidez (ya cubierta en test_execution_pipeline.py)."""
    return RiskManager(RiskLimits(
        max_vega_total=1e9, max_gamma_total=1e9,
        max_spread_relative=1.0, min_book_size=0.0, min_daily_volume=0.0,
    ))


def _quote(symbol, strike, iv, spot_ref, days_biz, expiry=date(2026, 9, 4),
           option_type=OptionType.CALL, greeks=None, bid=95.0, ask=105.0,
           bid_size=100.0, ask_size=100.0, as_of=None):
    book_kwargs = dict(bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size, last_volume=1000.0)
    if as_of is not None:
        book_kwargs["as_of"] = as_of
    book = OrderBookSnapshot(symbol, **book_kwargs)
    q = OptionQuote(symbol, strike=strike, expiry=expiry, option_type=option_type,
                     book=book, days_calendar=days_biz + 2, days_business=days_biz)
    q.iv = iv
    q.spot_ref = spot_ref
    q.greeks = greeks
    return q


def _default_config(**overrides) -> LongFirstConfig:
    cfg = LongFirstConfig(
        max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, min_contracts_per_trade=1,
        weekly_target_ars=1_000_000.0, max_holding_business_days=5, weekend_theta_guard_enabled=True,
        stop_loss_pct=0.50, take_profit_pct=1.00, smile_threshold_vol_points=3.0,
        moneyness_band_pct=0.15, require_level_confirmation=False, level_threshold_vol_points=5.0,
        enable_spread_completion=True, spread_wing_moneyness_pct=0.05,
        enable_obi_filter=True, min_obi_for_entry=-0.30,
        enable_vega_decay_exit=True, vega_decay_exit_ratio=0.35,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ---------------------------------------------------------------------------
# risk/position_sizer.py
# ---------------------------------------------------------------------------

def test_position_sizer_applies_floor_division_formula():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    # capital_asignado = 1,000,000 * 0.20 = 200,000; prima=350 -> costo/contrato=35,000
    # 200,000 / 35,000 = 5.71... -> floor = 5
    result = sizer.compute_contracts(premium_price=350.0)
    assert result.contracts == 5
    assert result.capital_allocated_ars == 200_000.0
    assert result.capital_used_ars == 5 * 350.0 * 100.0
    assert result.is_tradeable


def test_position_sizer_rejects_when_capital_insufficient_for_one_contract():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    # capital_asignado = 200,000; prima=3000 -> costo/contrato=300,000 > 200,000
    result = sizer.compute_contracts(premium_price=3000.0)
    assert result.contracts == 0
    assert not result.is_tradeable
    assert result.rejected_reason is not None


def test_position_sizer_never_exceeds_max_capital_ceiling():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=1.0, option_multiplier=100.0)
    # Aunque el llamador pase un capital_available_ars mas alto por error,
    # nunca debe usarse mas que max_capital_ars.
    result = sizer.compute_contracts(premium_price=100.0, capital_available_ars=50_000_000.0)
    assert result.capital_allocated_ars == 1_000_000.0  # techo, no 50M


def test_position_sizer_rejects_invalid_premium():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    assert sizer.compute_contracts(premium_price=0.0).contracts == 0
    assert sizer.compute_contracts(premium_price=-5.0).contracts == 0


# ---------------------------------------------------------------------------
# risk/risk_manager.py: evaluate_position_exit
# ---------------------------------------------------------------------------

def test_evaluate_position_exit_triggers_stop_loss():
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)  # miercoles
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=40.0,  # -60% sobre la prima
        entry_time=now - timedelta(hours=2), now=now, expiry=date(2026, 9, 4),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
    )
    assert reason == "stop_loss"


def test_evaluate_position_exit_triggers_take_profit():
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=210.0,  # +110%
        entry_time=now - timedelta(hours=2), now=now, expiry=date(2026, 9, 4),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
    )
    assert reason == "take_profit"


def test_evaluate_position_exit_triggers_weekly_horizon_expired():
    risk_mgr = RiskManager(RiskLimits())
    entry_time = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)  # lunes
    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)         # lunes siguiente: 5 ruedas habiles despues
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=105.0,  # dentro de banda de SL/TP
        entry_time=entry_time, now=now, expiry=date(2026, 10, 16),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
        weekend_theta_guard_enabled=False,
    )
    assert reason == "weekly_horizon_expired"


def test_evaluate_position_exit_triggers_weekend_theta_guard_on_friday():
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # viernes
    assert now.weekday() == 4
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=105.0, entry_time=now - timedelta(hours=1),
        now=now, expiry=date(2026, 9, 4),  # vence la semana siguiente, no hoy
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
        weekend_theta_guard_enabled=True,
    )
    assert reason == "weekend_theta_guard"


def test_evaluate_position_exit_weekend_guard_skipped_if_expires_same_friday():
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # viernes, y vence hoy mismo
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=105.0, entry_time=now - timedelta(hours=1),
        now=now, expiry=date(2026, 8, 28),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
        weekend_theta_guard_enabled=True,
    )
    assert reason is None  # se resuelve por vencimiento, no hace falta forzar nada


def test_evaluate_position_exit_returns_none_within_all_bands():
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)  # miercoles
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=105.0, entry_time=now - timedelta(hours=1),
        now=now, expiry=date(2026, 9, 4),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
    )
    assert reason is None


def test_evaluate_position_exit_handles_missing_current_price():
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=None, entry_time=now - timedelta(hours=1),
        now=now, expiry=date(2026, 9, 4),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
    )
    assert reason is None


def test_evaluate_vega_decay_exit_triggers_below_threshold():
    risk_mgr = RiskManager(RiskLimits())
    reason = risk_mgr.evaluate_vega_decay_exit(entry_vega=10.0, current_vega=3.0, decay_ratio_threshold=0.35)
    assert reason == "vega_theta_decay"


def test_evaluate_vega_decay_exit_does_not_trigger_above_threshold():
    risk_mgr = RiskManager(RiskLimits())
    reason = risk_mgr.evaluate_vega_decay_exit(entry_vega=10.0, current_vega=6.0, decay_ratio_threshold=0.35)
    assert reason is None


def test_evaluate_vega_decay_exit_boundary_is_inclusive():
    risk_mgr = RiskManager(RiskLimits())
    reason = risk_mgr.evaluate_vega_decay_exit(entry_vega=10.0, current_vega=3.5, decay_ratio_threshold=0.35)
    assert reason == "vega_theta_decay"


def test_evaluate_vega_decay_exit_handles_missing_values():
    risk_mgr = RiskManager(RiskLimits())
    assert risk_mgr.evaluate_vega_decay_exit(entry_vega=None, current_vega=3.0) is None
    assert risk_mgr.evaluate_vega_decay_exit(entry_vega=10.0, current_vega=None) is None
    assert risk_mgr.evaluate_vega_decay_exit(entry_vega=0.0, current_vega=3.0) is None


def test_evaluate_vega_decay_exit_sign_agnostic():
    """El signo de vega no importa (puts tienen vega positivo tambien en la convencion de este proyecto,
    pero el chequeo debe ser robusto a cualquier signo): se compara |current|/|entry|."""
    risk_mgr = RiskManager(RiskLimits())
    reason = risk_mgr.evaluate_vega_decay_exit(entry_vega=-10.0, current_vega=-2.0, decay_ratio_threshold=0.35)
    assert reason == "vega_theta_decay"


# ---------------------------------------------------------------------------
# strategy/weekly_asymmetric.py: scan_entry_signals
# ---------------------------------------------------------------------------

def test_scan_entry_signals_emits_buy_signal_for_cheap_base_in_band_and_horizon():
    cfg = _default_config()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC4900O", 4900, 0.60, spot, days_biz=3),
        _quote("GFGC5050O", 5050, 0.57, spot, days_biz=3),
        _quote("GFGC5200O", 5200, 0.45, spot, days_biz=3),   # target: bien barata
        _quote("GFGC5350O", 5350, 0.57, spot, days_biz=3),
        _quote("GFGC5500O", 5500, 0.60, spot, days_biz=3),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )

    symbols = {s.symbol for s in signals}
    assert "GFGC5200O" in symbols
    target = next(s for s in signals if s.symbol == "GFGC5200O")
    assert target.action == "buy_to_open"
    assert target.option_type is OptionType.CALL
    assert target.days_business_to_expiry == 3
    assert target.trend_context == "BULLISH"


def test_scan_entry_signals_never_emits_signal_for_expensive_base():
    """
    Invariante central del modo Long-First: una base 'cara' (IV por ENCIMA
    de la curva) nunca debe generar señal - eso seria abrir vendiendo, que
    es exactamente la venta en descubierto que este modo prohibe.
    """
    cfg = _default_config()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC4900O", 4900, 0.55, spot, days_biz=3),
        _quote("GFGC5050O", 5050, 0.55, spot, days_biz=3),
        _quote("GFGC5200O", 5200, 0.70, spot, days_biz=3),   # target: bien cara
        _quote("GFGC5350O", 5350, 0.55, spot, days_biz=3),
        _quote("GFGC5500O", 5500, 0.55, spot, days_biz=3),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert all(s.symbol != "GFGC5200O" for s in signals)
    assert all(s.action == "buy_to_open" for s in signals)  # ninguna señal generada es de venta


def test_scan_entry_signals_excludes_bases_beyond_weekly_horizon():
    cfg = _default_config(max_holding_business_days=5)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC4900O", 4900, 0.60, spot, days_biz=10),
        _quote("GFGC5050O", 5050, 0.57, spot, days_biz=10),
        _quote("GFGC5200O", 5200, 0.45, spot, days_biz=10),  # barata pero FUERA del horizonte semanal
        _quote("GFGC5350O", 5350, 0.57, spot, days_biz=10),
        _quote("GFGC5500O", 5500, 0.60, spot, days_biz=10),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert signals == []


def test_scan_entry_signals_excludes_bases_outside_moneyness_band():
    cfg = _default_config(moneyness_band_pct=0.15)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC4400O", 4400, 0.45, spot, days_biz=3),   # barata, pero muy OTM (fuera de banda)
        _quote("GFGC5000O", 5000, 0.58, spot, days_biz=3),
        _quote("GFGC5100O", 5100, 0.56, spot, days_biz=3),
        _quote("GFGC5300O", 5300, 0.56, spot, days_biz=3),
        _quote("GFGC6100O", 6100, 0.58, spot, days_biz=3),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert all(s.symbol != "GFGC4400O" for s in signals)


def test_scan_entry_signals_ranks_by_convexity_score_descending():
    cfg = _default_config()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0

    # Smile sintetico con curvatura real (no solo 4 puntos, que un ajuste
    # cuadratico de 3 parametros pasa casi exacto por todos y deja
    # dislocaciones ~0): con suficientes puntos de relleno la curva queda
    # bien determinada y las dos bases "target" se paran bien por debajo,
    # garantizando que ambas pasan el filtro de "barata" (< -smile_threshold)
    # y que lo unico que decide el orden es el score de convexidad.
    def smile_iv(strike: float) -> float:
        x = math.log(strike / spot)
        return 0.45 + 6.0 * x * x

    filler_strikes = [4700, 4900, 5000, 5100, 5300, 5400, 5500, 5700]
    filler = [_quote(f"GFGC{k}O", k, smile_iv(k), spot, days_biz=3) for k in filler_strikes]

    low_convexity = _quote(
        "GFGC5150O", 5150, smile_iv(5150) - 0.08, spot, days_biz=3,
        greeks={"gamma": 0.0005, "vega": 1.0, "delta": 0.5, "theta": -1.0},
    )
    high_convexity = _quote(
        "GFGC5250O", 5250, smile_iv(5250) - 0.08, spot, days_biz=3,
        greeks={"gamma": 0.01, "vega": 5.0, "delta": 0.5, "theta": -1.0},
    )
    quotes = [low_convexity, high_convexity] + filler
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )

    ranked_symbols = [s.symbol for s in signals if s.symbol in ("GFGC5150O", "GFGC5250O")]
    assert ranked_symbols == ["GFGC5250O", "GFGC5150O"]  # mayor convexidad primero


# ---------------------------------------------------------------------------
# strategy/weekly_asymmetric.py: scan_spread_completion_signals
# ---------------------------------------------------------------------------

def _chain_with_call_wing():
    chain = OptionChain()
    long_call = _quote("GFGC5200O", 5200, 0.55, 5200.0, days_biz=3)
    wing_call = _quote("GFGC5400O", 5400, 0.50, 5200.0, days_biz=3)
    chain.upsert_quote(long_call)
    chain.upsert_quote(wing_call)
    return chain, long_call, wing_call


def test_scan_spread_completion_signals_empty_without_confirmed_long_position():
    chain, long_call, _ = _chain_with_call_wing()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config())
    portfolio = Portfolio()  # sin ninguna posicion
    signals = strategy.scan_spread_completion_signals(chain, portfolio, trend="BULLISH")
    assert signals == []


def test_scan_spread_completion_signals_requires_positive_quantity_not_just_any_position():
    chain, long_call, _ = _chain_with_call_wing()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config())
    portfolio = Portfolio()
    portfolio.add(Position(symbol=long_call.symbol, quantity=-5, multiplier=100.0))  # corta, no larga
    signals = strategy.scan_spread_completion_signals(chain, portfolio, trend="BULLISH")
    assert signals == []  # una posicion corta NO habilita completar el spread


def test_scan_spread_completion_signals_picks_further_otm_wing_for_bull_call_spread():
    chain, long_call, wing_call = _chain_with_call_wing()
    # wing a 5400 vs. long a 5200 es ~3.8% de diferencia de strike; se
    # overridea spread_wing_moneyness_pct (default 5%) a un valor mas chico
    # para que esta ala puntual quede dentro de la banda buscada.
    strategy = WeeklyAsymmetricStrategy(
        _lenient_risk_manager(), config=_default_config(spread_wing_moneyness_pct=0.02),
    )
    portfolio = Portfolio()
    portfolio.add(Position(symbol=long_call.symbol, quantity=5, multiplier=100.0))

    signals = strategy.scan_spread_completion_signals(chain, portfolio, trend="BULLISH")
    assert len(signals) == 1
    signal = signals[0]
    assert signal.long_symbol == "GFGC5200O"
    assert signal.short_symbol == "GFGC5400O"
    assert signal.long_quantity_confirmed == 5
    assert "Bull Call Spread" in signal.reason
    assert signal.trend_context == "BULLISH"


def test_scan_spread_completion_signals_excludes_stale_wing_candidate():
    """
    Regresion del hallazgo del 2026-08-31 (ver RiskConfig.
    max_option_quote_staleness_seconds y el docstring de
    WeeklyAsymmetricStrategy._find_wing_quote): antes, el "wing" que
    completa el spread se elegia sin mirar la antiguedad de su cotizacion -
    a diferencia de las entradas nuevas (paso 2), que ya excluyen opciones
    stale. Este test simula exactamente el caso real observado (cadena de
    opciones caida sola, ver docs/AUDITORIA_MAESTRA_2026-08-27.md,
    seguimiento del 2026-08-31): el UNICO wing disponible tiene una punta de
    hace 10 minutos (por encima del umbral de 90s) - sin la guardia, se
    completaria el spread contra ese precio viejo; con la guardia, no se
    encuentra ningun wing valido y no se genera señal.
    """
    now = time.time()
    chain = OptionChain()
    long_call = _quote("GFGC5200O", 5200, 0.55, 5200.0, days_biz=3)
    stale_wing = _quote("GFGC5400O", 5400, 0.50, 5200.0, days_biz=3, as_of=now - 600.0)
    chain.upsert_quote(long_call)
    chain.upsert_quote(stale_wing)

    strategy = WeeklyAsymmetricStrategy(
        _lenient_risk_manager(), config=_default_config(spread_wing_moneyness_pct=0.02),
    )
    portfolio = Portfolio()
    portfolio.add(Position(symbol=long_call.symbol, quantity=5, multiplier=100.0))

    # Sin threshold (comportamiento por defecto, sin cambios): SI completa el spread.
    signals_no_guard = strategy.scan_spread_completion_signals(chain, portfolio, trend="BULLISH")
    assert len(signals_no_guard) == 1

    # Con la guardia activa: el unico wing disponible es stale -> no hay señal.
    signals_guarded = strategy.scan_spread_completion_signals(
        chain, portfolio, trend="BULLISH", max_quote_age_seconds=90.0, now=now,
    )
    assert signals_guarded == []

    # Si el wing esta fresco (as_of default = ahora), la guardia no bloquea nada.
    chain_fresh = OptionChain()
    fresh_wing = _quote("GFGC5400O", 5400, 0.50, 5200.0, days_biz=3)
    chain_fresh.upsert_quote(long_call)
    chain_fresh.upsert_quote(fresh_wing)
    signals_fresh = strategy.scan_spread_completion_signals(
        chain_fresh, portfolio, trend="BULLISH", max_quote_age_seconds=90.0, now=now,
    )
    assert len(signals_fresh) == 1


def test_scan_spread_completion_signals_forced_expiry_ignores_other_expiries():
    """
    Feature nueva (2026-09-01, a pedido explicito del usuario -
    GGAL_BOT_FORCE_EXPIRY / InstrumentsConfig.forced_expiry, ver run_bot.py
    __init__ y _run_weekly_asymmetric_cycle): cuando se pasa `forced_expiry`,
    scan_spread_completion_signals debe ignorar POR COMPLETO cualquier
    posicion larga confirmada y cualquier wing candidato de otro
    vencimiento - incluso si esa otra posicion tiene un wing perfectamente
    valido disponible. Simula dos vencimientos con una larga confirmada cada
    uno; sin forced_expiry ambos completan spread, con
    forced_expiry=Setiembre solo debe completarse el de Setiembre.
    """
    chain = OptionChain()
    long_call_sep = _quote("GFGC5200O", 5200, 0.55, 5200.0, days_biz=3, expiry=date(2026, 9, 4))
    wing_call_sep = _quote("GFGC5400O", 5400, 0.50, 5200.0, days_biz=3, expiry=date(2026, 9, 4))
    long_call_oct = _quote("GFGC5200OC", 5200, 0.55, 5200.0, days_biz=33, expiry=date(2026, 10, 16))
    wing_call_oct = _quote("GFGC5400OC", 5400, 0.50, 5200.0, days_biz=33, expiry=date(2026, 10, 16))
    for q in (long_call_sep, wing_call_sep, long_call_oct, wing_call_oct):
        chain.upsert_quote(q)

    strategy = WeeklyAsymmetricStrategy(
        _lenient_risk_manager(), config=_default_config(spread_wing_moneyness_pct=0.02),
    )
    portfolio = Portfolio()
    portfolio.add(Position(symbol=long_call_sep.symbol, quantity=5, multiplier=100.0))
    portfolio.add(Position(symbol=long_call_oct.symbol, quantity=5, multiplier=100.0))

    # Sin forced_expiry (comportamiento por defecto): ambos vencimientos completan su spread.
    signals_all = strategy.scan_spread_completion_signals(chain, portfolio, trend="BULLISH")
    assert len(signals_all) == 2

    # Con forced_expiry=Setiembre: Octubre se ignora por completo, aunque tenga
    # una larga confirmada y un wing perfectamente valido disponibles.
    signals_forced = strategy.scan_spread_completion_signals(
        chain, portfolio, trend="BULLISH", forced_expiry=date(2026, 9, 4),
    )
    assert len(signals_forced) == 1
    assert signals_forced[0].long_symbol == "GFGC5200O"
    assert signals_forced[0].short_symbol == "GFGC5400O"


def test_scan_spread_completion_signals_picks_lower_strike_wing_for_bear_put_spread():
    chain = OptionChain()
    long_put = _quote("GFGV5200O", 5200, 0.55, 5200.0, days_biz=3, option_type=OptionType.PUT)
    wing_put = _quote("GFGV5000O", 5000, 0.60, 5200.0, days_biz=3, option_type=OptionType.PUT)
    chain.upsert_quote(long_put)
    chain.upsert_quote(wing_put)

    strategy = WeeklyAsymmetricStrategy(
        _lenient_risk_manager(), config=_default_config(spread_wing_moneyness_pct=0.02),
    )
    portfolio = Portfolio()
    portfolio.add(Position(symbol=long_put.symbol, quantity=3, multiplier=100.0))

    signals = strategy.scan_spread_completion_signals(chain, portfolio, trend="BEARISH")
    assert len(signals) == 1
    assert signals[0].short_symbol == "GFGV5000O"
    assert "Bear Put Spread" in signals[0].reason


# ---------------------------------------------------------------------------
# Filtro direccional tecnico (ver data/technical_analysis.py): BULLISH solo
# Calls, BEARISH solo Puts, NEUTRAL exige dislocacion extrema y nunca
# completa spreads. Estos tests aislan especificamente ESE comportamiento
# (los de arriba ya cubren horizonte/moneyness/convexidad/spreads en si).
# ---------------------------------------------------------------------------

def _bullish_smile_quotes(spot=5200.0, days_biz=3):
    """Smile con una base CALL y una PUT igualmente 'baratas' (misma dislocacion), para
    aislar el efecto del filtro de tendencia sin que la dislocacion de smile decida nada."""
    def smile_iv(strike: float) -> float:
        x = math.log(strike / spot)
        return 0.45 + 6.0 * x * x

    filler_strikes = [4700, 4900, 5000, 5100, 5300, 5400, 5500, 5700]
    calls = [_quote(f"GFGC{k}O", k, smile_iv(k), spot, days_biz=days_biz) for k in filler_strikes]
    puts = [
        _quote(f"GFGV{k}O", k, smile_iv(k), spot, days_biz=days_biz, option_type=OptionType.PUT)
        for k in filler_strikes
    ]
    cheap_call = _quote("GFGC5150O", 5150, smile_iv(5150) - 0.05, spot, days_biz=days_biz)
    cheap_put = _quote("GFGV5150O", 5150, smile_iv(5150) - 0.05, spot, days_biz=days_biz, option_type=OptionType.PUT)
    return calls + puts + [cheap_call, cheap_put]


def test_scan_entry_signals_bullish_trend_only_considers_calls():
    cfg = _default_config()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)
    assert all(s.option_type is OptionType.CALL for s in signals)
    assert not any(s.symbol == "GFGV5150O" for s in signals)


def test_scan_entry_signals_bearish_trend_only_considers_puts():
    cfg = _default_config()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BEARISH",
    )
    assert any(s.symbol == "GFGV5150O" for s in signals)
    assert all(s.option_type is OptionType.PUT for s in signals)
    assert not any(s.symbol == "GFGC5150O" for s in signals)


def test_scan_entry_signals_neutral_trend_requires_extreme_dislocation():
    """
    Bajo NEUTRAL, una base que pasaria el umbral NORMAL (3.0 vol pts) pero
    no el umbral extremo (3.0 * neutral_extreme_smile_multiplier=2.0 -> 6.0)
    NO debe generar señal - "cash/espera" salvo dislocacion extrema.
    """
    cfg = _default_config()  # smile_threshold_vol_points=3.0
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()  # dislocacion de las bases "cheap_*" ronda -3.9 vol pts
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="NEUTRAL",
    )
    assert signals == []  # -3.9 supera el umbral normal (3.0) pero no el extremo (6.0)


def test_scan_entry_signals_neutral_trend_allows_truly_extreme_dislocation():
    cfg = _default_config()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    # Se agrega una base EXTREMADAMENTE barata (muy por debajo del umbral
    # extremo de -6.0), que si debe pasar incluso bajo NEUTRAL.
    spot = 5200.0

    def smile_iv(strike: float) -> float:
        x = math.log(strike / spot)
        return 0.45 + 6.0 * x * x

    extreme_call = _quote("GFGC5250O", 5250, smile_iv(5250) - 0.20, spot, days_biz=3)
    quotes = quotes + [extreme_call]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="NEUTRAL",
    )
    assert any(s.symbol == "GFGC5250O" for s in signals)


def test_scan_entry_signals_diagnostics_report_closest_miss_under_neutral_trend():
    """
    EntryScanDiagnostics (agregado a pedido explicito, ver seguimiento de
    auditoria del 2026-09-01 - duda sobre si los filtros son "muy duros"):
    debe reportar, SIN cambiar el resultado de scan_entry_signals(), que
    las 18 cotizaciones llegaron al chequeo de dislocacion (ningun filtro
    anterior las bloquea en este fixture), que ninguna califico bajo el
    umbral extremo de NEUTRAL, y cual fue la mas cerca de calificar.
    """
    cfg = _default_config()  # smile_threshold_vol_points=3.0
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()  # dislocacion de "cheap_*" ronda -3.9 vol pts
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="NEUTRAL",
    )
    assert signals == []  # comportamiento sin cambios (mismo test que arriba)

    diag = strategy.last_scan_diagnostics
    assert diag is not None
    assert diag.total_quotes == len(quotes) == 18
    assert diag.blocked_by_direction == 0  # NEUTRAL nunca descarta option_type de antemano
    assert diag.blocked_by_holding_days == 0
    assert diag.blocked_by_liquidity == 0
    assert diag.blocked_by_obi == 0
    assert diag.blocked_by_moneyness == 0
    assert diag.evaluated_for_dislocation == 18  # todas llegaron al chequeo de smile
    assert diag.blocked_by_dislocation == 18     # ninguna alcanzo el umbral extremo (6.0)
    assert diag.qualified == 0
    # Las bases "cheap_*" (~-3.9 vol pts) son las mas cercanas a calificar,
    # muy por delante de las bases de relleno (dislocacion ~0).
    assert diag.closest_miss_symbol in ("GFGC5150O", "GFGV5150O")
    assert diag.closest_miss_threshold_required == -6.0  # 3.0 * neutral_extreme_smile_multiplier=2.0
    assert 0.0 < diag.closest_miss_shortfall_vol_points < 3.0


def test_scan_entry_signals_diagnostics_identify_earlier_filter_as_bottleneck():
    """
    Cuando NINGUNA cotizacion llega siquiera al chequeo de dislocacion (acá,
    todas quedan afuera de la banda de moneyness), evaluated_for_dislocation
    debe quedar en 0 y blocked_by_moneyness debe explicar el motivo real -
    para no confundir "el umbral de smile es muy duro" con "el problema esta
    en otro filtro anterior".
    """
    cfg = _default_config(moneyness_band_pct=0.001)  # banda absurdamente angosta a proposito
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="NEUTRAL",
    )
    assert signals == []

    diag = strategy.last_scan_diagnostics
    assert diag is not None
    assert diag.evaluated_for_dislocation == 0
    assert diag.blocked_by_dislocation == 0
    assert diag.blocked_by_moneyness == diag.total_quotes  # el cuello de botella real
    assert diag.closest_miss_symbol is None  # nunca se llego a medir dislocacion


def test_scan_entry_signals_technical_filter_disabled_ignores_trend():
    """Con GGAL_BOT_TECHNICAL_FILTER_ENABLED=false, el comportamiento debe ser identico al de antes de este modulo."""
    original_enabled = SETTINGS.technical_analysis.enabled
    SETTINGS.technical_analysis.enabled = False
    try:
        cfg = _default_config()
        strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
        quotes = _bullish_smile_quotes()
        surface = VolatilitySurface(quotes)
        # trend="BEARISH" pero el filtro esta apagado: las CALLS baratas
        # (GFGC5150O) igual deberian poder generar señal.
        signals = strategy.scan_entry_signals(
            surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BEARISH",
        )
        assert any(s.symbol == "GFGC5150O" for s in signals)
    finally:
        SETTINGS.technical_analysis.enabled = original_enabled


def _extreme_call_quote(spot=5200.0):
    """
    Base CALL con dislocacion por debajo del umbral extremo, calibrada para
    las pruebas de Momentum Shift de abajo (distinta del discount de 0.20 en
    test_scan_entry_signals_neutral_trend_allows_truly_extreme_dislocation):
    esta base se agrega a la MISMA superficie que _bullish_smile_quotes(),
    y VolatilitySurface ajusta el smile de forma CONJUNTA sobre todos los
    puntos - un discount demasiado grande distorsiona el ajuste cuadratico
    para el resto de las bases (incluida GFGV5150O/GFGC5150O), no solo para
    esta. 0.09 fue verificado numericamente: deja esta base en aprox. -7.3
    vol pts (bajo el umbral extremo de 6.0) sin arrastrar a GFGC5150O/
    GFGV5150O (aprox. -3.2, siguen por encima del extremo aunque un poco por
    debajo del normal de 3.0) fuera de sus umbrales esperados.
    """
    def smile_iv(strike: float) -> float:
        x = math.log(strike / spot)
        return 0.45 + 6.0 * x * x

    return _quote("GFGC5250O", 5250, smile_iv(5250) - 0.09, spot, days_biz=3)


def test_scan_entry_signals_momentum_shift_allows_contrarian_type_only_at_extreme_threshold():
    """
    Bajo BEARISH con momentum_shift=EARLY_BULLISH_REVERSAL: el tipo CALL
    (contrario a BEARISH) deja de descartarse de plano, pero SOLO pasa si la
    dislocacion es EXTREMA (>= umbral*neutral_extreme_smile_multiplier) - la
    base "cheap_call" (GFGC5150O, ~-3.9 vol pts, pasaria el umbral normal de
    3.0 pero no el extremo de 6.0) sigue sin generar señal, mientras que la
    base realmente extrema (GFGC5250O) si la genera. La PUT alineada con la
    tendencia (GFGV5150O) sigue evaluandose bajo el umbral NORMAL, sin cambios.
    """
    cfg = _default_config()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes() + [_extreme_call_quote()]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes},
        trend="BEARISH", momentum_shift=MomentumShift.EARLY_BULLISH_REVERSAL.value,
    )
    symbols = {s.symbol for s in signals}
    assert "GFGV5150O" in symbols       # PUT alineada, umbral normal: sin cambios
    assert "GFGC5150O" not in symbols   # CALL contraria, solo -3.9 vol pts: no alcanza el umbral extremo
    assert "GFGC5250O" in symbols       # CALL contraria, dislocacion realmente extrema: SI pasa


def test_scan_entry_signals_no_momentum_shift_still_bans_contrarian_type_even_if_extreme():
    """
    Sin momentum_shift (o con uno que no contradice la tendencia vigente), el
    tipo contrario sigue prohibido de plano bajo BULLISH/BEARISH, sin
    excepcion por dislocacion extrema - la extrema dislocacion NO alcanza por
    si sola, hace falta la señal de reversion temprana (contraste directo con
    la prueba de arriba, misma fixture).
    """
    cfg = _default_config()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes() + [_extreme_call_quote()]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BEARISH", momentum_shift=None,
    )
    symbols = {s.symbol for s in signals}
    assert "GFGC5250O" not in symbols
    assert all(s.option_type is OptionType.PUT for s in signals)


def test_scan_entry_signals_momentum_shift_override_disabled_by_config():
    """
    Con GGAL_BOT_TA_ENABLE_MOMENTUM_OVERRIDE=false, un momentum_shift
    contrario a la tendencia no debe relajar nada, aunque la dislocacion sea
    extrema - identico a no haber pasado momentum_shift.
    """
    original = SETTINGS.technical_analysis.enable_momentum_shift_override
    SETTINGS.technical_analysis.enable_momentum_shift_override = False
    try:
        cfg = _default_config()
        strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
        quotes = _bullish_smile_quotes() + [_extreme_call_quote()]
        surface = VolatilitySurface(quotes)
        signals = strategy.scan_entry_signals(
            surface, recent_volumes={q.symbol: 1000.0 for q in quotes},
            trend="BEARISH", momentum_shift=MomentumShift.EARLY_BULLISH_REVERSAL.value,
        )
        symbols = {s.symbol for s in signals}
        assert "GFGC5250O" not in symbols
        assert all(s.option_type is OptionType.PUT for s in signals)
    finally:
        SETTINGS.technical_analysis.enable_momentum_shift_override = original


def test_scan_entry_signals_momentum_shift_does_not_affect_neutral_behavior():
    """
    Bajo NEUTRAL, `momentum_shift` no cambia nada (la condicion de override
    solo aplica bajo BULLISH/BEARISH): ambos tipos ya se evaluaban bajo el
    umbral extremo de por si - se verifica que el resultado es identico con
    o sin un momentum_shift presente.
    """
    cfg = _default_config()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes() + [_extreme_call_quote()]
    surface = VolatilitySurface(quotes)
    volumes = {q.symbol: 1000.0 for q in quotes}
    signals_without = strategy.scan_entry_signals(surface, recent_volumes=volumes, trend="NEUTRAL", momentum_shift=None)
    signals_with = strategy.scan_entry_signals(
        surface, recent_volumes=volumes, trend="NEUTRAL",
        momentum_shift=MomentumShift.EARLY_BULLISH_REVERSAL.value,
    )
    assert {s.symbol for s in signals_without} == {s.symbol for s in signals_with}
    assert {s.symbol for s in signals_without} == {"GFGC5250O"}  # solo la realmente extrema pasa


def test_scan_spread_completion_signals_neutral_trend_never_completes_spreads():
    chain, long_call, _ = _chain_with_call_wing()
    strategy = WeeklyAsymmetricStrategy(
        _lenient_risk_manager(), config=_default_config(spread_wing_moneyness_pct=0.02),
    )
    portfolio = Portfolio()
    portfolio.add(Position(symbol=long_call.symbol, quantity=5, multiplier=100.0))
    signals = strategy.scan_spread_completion_signals(chain, portfolio, trend="NEUTRAL")
    assert signals == []


def test_scan_spread_completion_signals_bearish_trend_ignores_call_spread():
    """Una larga CALL confirmada no debe completarse en spread si la tendencia vigente es BEARISH (contraria)."""
    chain, long_call, _ = _chain_with_call_wing()
    strategy = WeeklyAsymmetricStrategy(
        _lenient_risk_manager(), config=_default_config(spread_wing_moneyness_pct=0.02),
    )
    portfolio = Portfolio()
    portfolio.add(Position(symbol=long_call.symbol, quantity=5, multiplier=100.0))
    signals = strategy.scan_spread_completion_signals(chain, portfolio, trend="BEARISH")
    assert signals == []


def test_scan_spread_completion_signals_disabled_by_config():
    chain, long_call, _ = _chain_with_call_wing()
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config(enable_spread_completion=False))
    portfolio = Portfolio()
    portfolio.add(Position(symbol=long_call.symbol, quantity=5, multiplier=100.0))
    assert strategy.scan_spread_completion_signals(chain, portfolio) == []


# ---------------------------------------------------------------------------
# strategy/weekly_asymmetric.py: build_exit_signals
# ---------------------------------------------------------------------------

def test_build_exit_signals_skips_positions_missing_entry_metadata():
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config())
    portfolio = Portfolio()
    portfolio.add(Position(symbol="GFGC5200O", quantity=5, multiplier=100.0))  # sin entry_price/entry_time
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 40.0}, now=now)
    assert signals == []


def test_build_exit_signals_ignores_non_long_positions():
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config())
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=-5, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 10.0}, now=now)
    assert signals == []  # long-only: una posicion corta (residual/legacy) no se gestiona aca


def test_build_exit_signals_produces_stop_loss_signal():
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config(stop_loss_pct=0.50))
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 40.0}, now=now)
    assert len(signals) == 1
    assert signals[0].reason == "stop_loss"
    assert signals[0].action == "sell_to_close"
    assert signals[0].quantity == 5


# ---------------------------------------------------------------------------
# Confirmacion de microestructura (Order Book Imbalance, ver
# models/microstructure.py) y salida por compresion de vega (ver
# risk_manager.evaluate_vega_decay_exit) - las dos mejoras cuantitativas
# agregadas sobre el chasis de WeeklyAsymmetricStrategy (ver seccion "Hybrid
# Trend-Aligned Skew Reversion" en README.md para el razonamiento completo).
# ---------------------------------------------------------------------------

def test_scan_entry_signals_obi_filter_blocks_extreme_sell_side_imbalance():
    """
    Una base barata (dislocacion suficiente) pero con el libro fuertemente
    desbalanceado hacia el lado vendedor (ask_size >> bid_size, OBI muy
    negativo) NO debe generar señal: es exactamente el caso que el filtro
    de calidad de ejecucion esta pensado para bloquear.
    """
    cfg = _default_config(min_obi_for_entry=-0.30)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    # La base barata (GFGC5150O) queda con OBI = (10-490)/(10+490) = -0.96,
    # muy por debajo del piso de -0.30.
    for q in quotes:
        if q.symbol == "GFGC5150O":
            q.book.bid_size = 10.0
            q.book.ask_size = 490.0
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert not any(s.symbol == "GFGC5150O" for s in signals)


def test_scan_entry_signals_obi_filter_allows_normal_imbalance():
    """Un desbalance moderado (por encima del piso configurado) no debe bloquear la señal."""
    cfg = _default_config(min_obi_for_entry=-0.30)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    for q in quotes:
        if q.symbol == "GFGC5150O":
            q.book.bid_size = 80.0
            q.book.ask_size = 120.0  # OBI = -0.20, por encima del piso -0.30
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)


def test_scan_entry_signals_obi_filter_disabled_ignores_imbalance():
    """Con enable_obi_filter=False, un desbalance extremo no debe bloquear nada (comportamiento pre-modulo)."""
    cfg = _default_config(enable_obi_filter=False)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    for q in quotes:
        if q.symbol == "GFGC5150O":
            q.book.bid_size = 1.0
            q.book.ask_size = 999.0
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)


def test_build_exit_signals_vega_decay_triggers_when_convexity_exhausted():
    """
    |vega| actual = 20% del |vega| de entrada (por debajo del piso de 35%
    default): la tesis de convexidad ya se agoto, debe cerrar aunque el
    PnL% de la prima este dentro de banda (ni Stop Loss ni Take Profit).
    """
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config())
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        greeks_per_unit={"vega": 10.0, "gamma": 0.05, "delta": 0.5, "theta": -1.0},
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC5200O": 105.0}, now=now,
        current_greeks={"GFGC5200O": {"vega": 2.0, "gamma": 0.01, "delta": 0.8, "theta": -0.3}},
    )
    assert len(signals) == 1
    assert signals[0].reason == "vega_theta_decay"


def test_build_exit_signals_vega_decay_does_not_trigger_above_threshold():
    """|vega| actual = 60% del de entrada (por encima del piso de 35%): no debe cerrar por esta regla."""
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config())
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        greeks_per_unit={"vega": 10.0, "gamma": 0.05, "delta": 0.5, "theta": -1.0},
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC5200O": 105.0}, now=now,
        current_greeks={"GFGC5200O": {"vega": 6.0, "gamma": 0.03, "delta": 0.7, "theta": -0.6}},
    )
    assert signals == []


def test_build_exit_signals_vega_decay_skipped_without_current_greeks():
    """Sin `current_greeks` (compatibilidad hacia atras), la regla de compresion de vega ni se evalua."""
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config())
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        greeks_per_unit={"vega": 10.0, "gamma": 0.05, "delta": 0.5, "theta": -1.0},
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 105.0}, now=now)
    assert signals == []


def test_build_exit_signals_vega_decay_disabled_by_config():
    """Con enable_vega_decay_exit=False, ni una compresion extrema (10%) debe disparar cierre."""
    cfg = _default_config(enable_vega_decay_exit=False)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        greeks_per_unit={"vega": 10.0, "gamma": 0.05, "delta": 0.5, "theta": -1.0},
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC5200O": 105.0}, now=now,
        current_greeks={"GFGC5200O": {"vega": 1.0, "gamma": 0.005, "delta": 0.9, "theta": -0.1}},
    )
    assert signals == []


def test_build_exit_signals_stop_loss_takes_priority_over_vega_decay():
    """
    Si Stop Loss YA dispara (PnL% de la prima), la salida por compresion de
    vega ni se evalua - evaluate_position_exit() sigue siendo la PRIMERA
    fuente de verdad, la compresion de vega es un chequeo secundario que
    solo corre cuando nada mas disparo todavia.
    """
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config(stop_loss_pct=0.50))
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        greeks_per_unit={"vega": 10.0, "gamma": 0.05, "delta": 0.5, "theta": -1.0},
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC5200O": 40.0}, now=now,  # -60%: dispara stop_loss
        current_greeks={"GFGC5200O": {"vega": 1.0, "gamma": 0.005, "delta": 0.9, "theta": -0.1}},  # tambien compresion extrema
    )
    assert len(signals) == 1
    assert signals[0].reason == "stop_loss"  # no "vega_theta_decay"


ALL_TESTS = [
    test_position_sizer_applies_floor_division_formula,
    test_position_sizer_rejects_when_capital_insufficient_for_one_contract,
    test_position_sizer_never_exceeds_max_capital_ceiling,
    test_position_sizer_rejects_invalid_premium,
    test_evaluate_position_exit_triggers_stop_loss,
    test_evaluate_position_exit_triggers_take_profit,
    test_evaluate_position_exit_triggers_weekly_horizon_expired,
    test_evaluate_position_exit_triggers_weekend_theta_guard_on_friday,
    test_evaluate_position_exit_weekend_guard_skipped_if_expires_same_friday,
    test_evaluate_position_exit_returns_none_within_all_bands,
    test_evaluate_position_exit_handles_missing_current_price,
    test_evaluate_vega_decay_exit_triggers_below_threshold,
    test_evaluate_vega_decay_exit_does_not_trigger_above_threshold,
    test_evaluate_vega_decay_exit_boundary_is_inclusive,
    test_evaluate_vega_decay_exit_handles_missing_values,
    test_evaluate_vega_decay_exit_sign_agnostic,
    test_scan_entry_signals_emits_buy_signal_for_cheap_base_in_band_and_horizon,
    test_scan_entry_signals_never_emits_signal_for_expensive_base,
    test_scan_entry_signals_excludes_bases_beyond_weekly_horizon,
    test_scan_entry_signals_excludes_bases_outside_moneyness_band,
    test_scan_entry_signals_ranks_by_convexity_score_descending,
    test_scan_spread_completion_signals_empty_without_confirmed_long_position,
    test_scan_spread_completion_signals_requires_positive_quantity_not_just_any_position,
    test_scan_spread_completion_signals_picks_further_otm_wing_for_bull_call_spread,
    test_scan_spread_completion_signals_forced_expiry_ignores_other_expiries,
    test_scan_spread_completion_signals_excludes_stale_wing_candidate,
    test_scan_spread_completion_signals_picks_lower_strike_wing_for_bear_put_spread,
    test_scan_entry_signals_bullish_trend_only_considers_calls,
    test_scan_entry_signals_bearish_trend_only_considers_puts,
    test_scan_entry_signals_neutral_trend_requires_extreme_dislocation,
    test_scan_entry_signals_neutral_trend_allows_truly_extreme_dislocation,
    test_scan_entry_signals_diagnostics_report_closest_miss_under_neutral_trend,
    test_scan_entry_signals_diagnostics_identify_earlier_filter_as_bottleneck,
    test_scan_entry_signals_technical_filter_disabled_ignores_trend,
    test_scan_entry_signals_momentum_shift_allows_contrarian_type_only_at_extreme_threshold,
    test_scan_entry_signals_no_momentum_shift_still_bans_contrarian_type_even_if_extreme,
    test_scan_entry_signals_momentum_shift_override_disabled_by_config,
    test_scan_entry_signals_momentum_shift_does_not_affect_neutral_behavior,
    test_scan_spread_completion_signals_neutral_trend_never_completes_spreads,
    test_scan_spread_completion_signals_bearish_trend_ignores_call_spread,
    test_scan_spread_completion_signals_disabled_by_config,
    test_build_exit_signals_skips_positions_missing_entry_metadata,
    test_build_exit_signals_ignores_non_long_positions,
    test_build_exit_signals_produces_stop_loss_signal,
    test_scan_entry_signals_obi_filter_blocks_extreme_sell_side_imbalance,
    test_scan_entry_signals_obi_filter_allows_normal_imbalance,
    test_scan_entry_signals_obi_filter_disabled_ignores_imbalance,
    test_build_exit_signals_vega_decay_triggers_when_convexity_exhausted,
    test_build_exit_signals_vega_decay_does_not_trigger_above_threshold,
    test_build_exit_signals_vega_decay_skipped_without_current_greeks,
    test_build_exit_signals_vega_decay_disabled_by_config,
    test_build_exit_signals_stop_loss_takes_priority_over_vega_decay,
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
