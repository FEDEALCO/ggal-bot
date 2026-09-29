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

# Debe importarse ANTES que run_bot/ggal_bot.execution.order_gateway (ver
# docstring de ese modulo) para redirigir el CSV de auditoria de shadow
# trading a un path temporal - necesario para los tests de
# GgalOptionsBot._act_on_exit_signal() de mas abajo (mismo criterio que
# test_scalping_mode.py).
from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS, LongFirstConfig
from ggal_bot.data.option_chain import OptionChain, OptionQuote, OrderBookSnapshot
from ggal_bot.data.technical_analysis import MomentumShift
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.portfolio.portfolio import Portfolio, Position
from ggal_bot.risk.position_sizer import PositionSizer
from ggal_bot.risk.risk_manager import RiskLimits, RiskManager
from ggal_bot.strategy.weekly_asymmetric import WeeklyAsymmetricStrategy
from run_bot import GgalOptionsBot


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
        # MEJORAS 2026-09-04: baseline explicitamente "sin efecto" (igual al
        # comportamiento previo a estas mejoras), independientemente de los
        # defaults de produccion en config.py (que SI las activan) - asi
        # ningun test existente que use este helper cambia de resultado;
        # los tests nuevos de cada mejora las activan explicitamente via
        # **overrides.
        enable_tiered_stop_loss=False,
        tiered_stop_loss_stage2_business_day=2, tiered_stop_loss_stage2_pct=0.35,
        tiered_stop_loss_stage3_business_day=4, tiered_stop_loss_stage3_pct=0.20,
        vega_decay_min_holding_hours=0.0,
        enable_partial_profit_take=False,
        partial_profit_trigger_pct=0.15, partial_profit_take_fraction=0.50,
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


def test_evaluate_position_exit_horizon_disabled_when_max_holding_business_days_is_none():
    """
    Regresion (2026-09-07, a pedido explicito del usuario - ver
    LongFirstConfig/ScalpingConfig.max_holding_business_days en config.py):
    con None ("sin limite"), la salida "weekly_horizon_expired" NUNCA debe
    dispararse, sin importar cuantos dias habiles lleve abierta la
    posicion. Mismo fixture EXACTO que
    test_evaluate_position_exit_triggers_weekly_horizon_expired (5 ruedas
    habiles mantenida) - unica diferencia: max_holding_business_days=None.
    """
    risk_mgr = RiskManager(RiskLimits())
    entry_time = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)  # lunes
    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)         # lunes siguiente: 5 ruedas habiles despues
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=105.0,  # dentro de banda de SL/TP
        entry_time=entry_time, now=now, expiry=date(2026, 10, 16),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=None,
        weekend_theta_guard_enabled=False,
    )
    assert reason is None


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


def test_evaluate_position_exit_weekend_guard_still_fires_when_cap_not_configured():
    """
    weekend_theta_guard_max_holding_business_days=None (default, TANDA 2
    2026-09-08) debe preservar EXACTAMENTE el comportamiento de siempre:
    el guard dispara sin excepcion, sin importar cuantos dias lleva
    abierta la posicion - regresion explicita para que nadie asuma que el
    parametro nuevo cambia algo sin configurarlo.
    """
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # viernes
    entry_time = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)  # lunes, 4 dias habiles antes
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=105.0, entry_time=entry_time,
        now=now, expiry=date(2026, 10, 16),  # vencimiento lejano (Octubre)
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=None,
        weekend_theta_guard_enabled=True, weekend_theta_guard_max_holding_business_days=None,
    )
    assert reason == "weekend_theta_guard"


def test_evaluate_position_exit_weekend_guard_exempts_position_past_configured_cap():
    """
    Con weekend_theta_guard_max_holding_business_days=4 y una posicion que
    ya lleva 4 dias habiles abierta (entro el lunes, hoy es viernes), el
    guard YA NO debe forzar el cierre - la politica nueva (config.py,
    TANDA 2) asume que a esa altura la posicion ya esta bajo un stop mas
    ajustado (tiered_stop_loss) y puede sobrevivir el fin de semana.
    """
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # viernes
    entry_time = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)  # lunes, 4 dias habiles antes
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=105.0, entry_time=entry_time,
        now=now, expiry=date(2026, 10, 16),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=None,
        weekend_theta_guard_enabled=True, weekend_theta_guard_max_holding_business_days=4,
    )
    assert reason is None


def test_evaluate_position_exit_weekend_guard_still_fires_below_configured_cap():
    """
    Simetrico al test anterior: con el mismo cap=4 pero una posicion que
    todavia lleva MENOS de 4 dias habiles (entro el martes, hoy es
    viernes = 3 dias habiles), el guard debe seguir disparando - la
    exencion nueva es estrictamente >= cap, nunca antes.
    """
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # viernes
    entry_time = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)  # martes, 3 dias habiles antes
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=105.0, entry_time=entry_time,
        now=now, expiry=date(2026, 10, 16),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=None,
        weekend_theta_guard_enabled=True, weekend_theta_guard_max_holding_business_days=4,
    )
    assert reason == "weekend_theta_guard"


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


def test_evaluate_position_exit_horizon_expired_fires_even_without_current_price():
    """
    Regresion (BUG REAL VERIFICADO 2026-09-08, ver reconciliation.py y el
    log de produccion tras el modo aditivo weekly_asymmetric+scalping):
    antes, `current_price is None` cortaba TODA la funcion (incluidos el
    horizonte semanal y la guardia de fin de semana, que son puramente
    calendario y no necesitan ningun precio) - una posicion sin cotizacion
    vigente (base iliquida, ver reconciliation.reconstruct_positions_from_
    shadow_log) quedaba sin NINGUNA gestion de riesgo en absoluto, ni
    siquiera el corte por horizonte. `max_holding_business_days=5` con 6
    dias habiles de holding debe disparar igual sin importar que no haya
    precio.
    """
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)  # miercoles
    entry_time = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)  # lunes de la semana anterior
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=None, entry_time=entry_time,
        now=now, expiry=date(2026, 10, 16),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
    )
    assert reason == "weekly_horizon_expired"


def test_evaluate_position_exit_weekend_guard_fires_even_without_current_price():
    """Mismo bug que el test de arriba, version guardia de fin de semana."""
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # viernes
    entry_time = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)  # miercoles, sin cap configurado
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=None, entry_time=entry_time,
        now=now, expiry=date(2026, 10, 16),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=None,
        weekend_theta_guard_enabled=True, weekend_theta_guard_max_holding_business_days=None,
    )
    assert reason == "weekend_theta_guard"


def test_evaluate_position_exit_still_returns_none_without_price_when_no_calendar_condition_met():
    """
    Complemento: sin precio, pero TAMPOCO ninguna condicion de calendario
    (ni horizonte ni fin de semana) - debe seguir devolviendo None, no
    "inventar" un cierre. Prueba que el fix es aditivo, no un cambio de
    comportamiento por default.
    """
    risk_mgr = RiskManager(RiskLimits())
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)  # miercoles
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=None, entry_time=now - timedelta(hours=1),
        now=now, expiry=date(2026, 9, 4),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
        weekend_theta_guard_enabled=True,
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


def test_evaluate_vega_decay_exit_blocked_before_min_holding_hours():
    """
    FLEXIBILIZACION 2026-09-04: aunque el vega ya se comprimio por debajo
    del umbral, si la posicion lleva MENOS del tiempo minimo configurado
    todavia no se fuerza el cierre - le da margen a la posicion para
    desarrollarse en vez de cortarla con PnL bajo apenas se abrio.
    """
    risk_mgr = RiskManager(RiskLimits())
    entry_time = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
    now = entry_time + timedelta(hours=1)  # 1h, por debajo del minimo de 3hs
    reason = risk_mgr.evaluate_vega_decay_exit(
        entry_vega=10.0, current_vega=1.0, decay_ratio_threshold=0.20,
        entry_time=entry_time, now=now, min_holding_hours=3.0,
    )
    assert reason is None


def test_evaluate_vega_decay_exit_allowed_after_min_holding_hours():
    """La misma compresion, pero ya paso el tiempo minimo: se dispara normalmente."""
    risk_mgr = RiskManager(RiskLimits())
    entry_time = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
    now = entry_time + timedelta(hours=4)  # 4h, por encima del minimo de 3hs
    reason = risk_mgr.evaluate_vega_decay_exit(
        entry_vega=10.0, current_vega=1.0, decay_ratio_threshold=0.20,
        entry_time=entry_time, now=now, min_holding_hours=3.0,
    )
    assert reason == "vega_theta_decay"


def test_evaluate_vega_decay_exit_min_holding_hours_ignored_without_time_args():
    """Backward-compat: sin entry_time/now (llamador que no los pasa), el gate de tiempo simplemente no aplica."""
    risk_mgr = RiskManager(RiskLimits())
    reason = risk_mgr.evaluate_vega_decay_exit(entry_vega=10.0, current_vega=1.0, decay_ratio_threshold=0.20)
    assert reason == "vega_theta_decay"


# ---------------------------------------------------------------------------
# risk/risk_manager.py: evaluate_position_exit - Stop Loss escalonado por
# dia habil (MEJORA 2026-09-04, ver docstring de evaluate_position_exit)
# ---------------------------------------------------------------------------

def test_evaluate_position_exit_tiered_stop_loss_stage1_uses_fixed_pct():
    """Dias held=1 (< stage2=2): el stop sigue siendo el -50% fijo, -40% todavia no dispara."""
    risk_mgr = RiskManager(RiskLimits())
    entry_time = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)  # lunes
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)         # martes: 1 dia habil despues
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=60.0,  # -40%
        entry_time=entry_time, now=now, expiry=date(2026, 9, 4),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
        weekend_theta_guard_enabled=False,
        enable_tiered_stop_loss=True,
        tiered_stop_loss_stage2_business_day=2, tiered_stop_loss_stage2_pct=0.35,
        tiered_stop_loss_stage3_business_day=4, tiered_stop_loss_stage3_pct=0.20,
    )
    assert reason is None


def test_evaluate_position_exit_tiered_stop_loss_stage2_narrows_threshold():
    """Dias held=2 (>= stage2): el stop se angosta a -35%, -40% ahora si dispara."""
    risk_mgr = RiskManager(RiskLimits())
    entry_time = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)  # lunes
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)         # miercoles: 2 dias habiles despues
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=60.0,  # -40%
        entry_time=entry_time, now=now, expiry=date(2026, 9, 4),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
        weekend_theta_guard_enabled=False,
        enable_tiered_stop_loss=True,
        tiered_stop_loss_stage2_business_day=2, tiered_stop_loss_stage2_pct=0.35,
        tiered_stop_loss_stage3_business_day=4, tiered_stop_loss_stage3_pct=0.20,
    )
    assert reason == "stop_loss"


def test_evaluate_position_exit_tiered_stop_loss_stage3_narrows_further():
    """Dias held=4 (>= stage3): el stop se angosta a -20%, una perdida de -25% ya dispara."""
    risk_mgr = RiskManager(RiskLimits())
    entry_time = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)  # lunes
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)         # viernes: 4 dias habiles despues
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=75.0,  # -25%
        entry_time=entry_time, now=now, expiry=date(2026, 9, 4),  # vence otra semana: no interfiere el guardia de viernes
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
        weekend_theta_guard_enabled=False,
        enable_tiered_stop_loss=True,
        tiered_stop_loss_stage2_business_day=2, tiered_stop_loss_stage2_pct=0.35,
        tiered_stop_loss_stage3_business_day=4, tiered_stop_loss_stage3_pct=0.20,
    )
    assert reason == "stop_loss"


def test_evaluate_position_exit_tiered_stop_loss_disabled_preserves_fixed_pct():
    """Con enable_tiered_stop_loss=False, el mismo caso de arriba (dias=4, -25%) NO dispara: sigue el -50% fijo."""
    risk_mgr = RiskManager(RiskLimits())
    entry_time = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    reason = risk_mgr.evaluate_position_exit(
        entry_price=100.0, current_price=75.0,  # -25%
        entry_time=entry_time, now=now, expiry=date(2026, 9, 4),
        stop_loss_pct=0.50, take_profit_pct=1.00, max_holding_business_days=5,
        weekend_theta_guard_enabled=False,
        enable_tiered_stop_loss=False,
    )
    assert reason is None


# ---------------------------------------------------------------------------
# risk/risk_manager.py: evaluate_partial_profit_take (MEJORA 2026-09-04)
# ---------------------------------------------------------------------------

def test_evaluate_partial_profit_take_triggers_above_threshold():
    risk_mgr = RiskManager(RiskLimits())
    assert risk_mgr.evaluate_partial_profit_take(
        entry_price=100.0, current_price=116.0, already_taken=False, trigger_pct=0.15,
    ) is True


def test_evaluate_partial_profit_take_boundary_is_inclusive():
    risk_mgr = RiskManager(RiskLimits())
    assert risk_mgr.evaluate_partial_profit_take(
        entry_price=100.0, current_price=115.0, already_taken=False, trigger_pct=0.15,
    ) is True


def test_evaluate_partial_profit_take_not_triggered_below_threshold():
    risk_mgr = RiskManager(RiskLimits())
    assert risk_mgr.evaluate_partial_profit_take(
        entry_price=100.0, current_price=110.0, already_taken=False, trigger_pct=0.15,
    ) is False


def test_evaluate_partial_profit_take_skipped_if_already_taken():
    """Ya se tomo antes para esta posicion: no se dispara de nuevo aunque el PnL% siga por encima del umbral."""
    risk_mgr = RiskManager(RiskLimits())
    assert risk_mgr.evaluate_partial_profit_take(
        entry_price=100.0, current_price=150.0, already_taken=True, trigger_pct=0.15,
    ) is False


def test_evaluate_partial_profit_take_handles_missing_values():
    risk_mgr = RiskManager(RiskLimits())
    assert risk_mgr.evaluate_partial_profit_take(entry_price=None, current_price=150.0, already_taken=False) is False
    assert risk_mgr.evaluate_partial_profit_take(entry_price=100.0, current_price=None, already_taken=False) is False
    assert risk_mgr.evaluate_partial_profit_take(entry_price=0.0, current_price=150.0, already_taken=False) is False


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


def test_scan_entry_signals_includes_bases_beyond_horizon_when_limit_disabled():
    """
    Regresion (2026-09-07, a pedido explicito del usuario: "quita el limite
    de vencimiento... el objetivo es que opere en el vto mas proximo de
    opciones que tenga mayor profundidad y liquidez, como en este caso el
    vto de octubre"). Mismo fixture EXACTO que el test de arriba (bases a
    days_biz=10, mas alla del horizonte de 5 que usaba ese test), pero con
    max_holding_business_days=None ("sin limite") - la base barata
    (GFGC5200O, iv=0.45) ahora SI debe calificar, porque nada la descarta
    por distancia al vencimiento.
    """
    cfg = _default_config(max_holding_business_days=None)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC4900O", 4900, 0.60, spot, days_biz=10),
        _quote("GFGC5050O", 5050, 0.57, spot, days_biz=10),
        _quote("GFGC5200O", 5200, 0.45, spot, days_biz=10),  # barata, antes excluida solo por horizonte
        _quote("GFGC5350O", 5350, 0.57, spot, days_biz=10),
        _quote("GFGC5500O", 5500, 0.60, spot, days_biz=10),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert len(signals) == 1
    assert signals[0].symbol == "GFGC5200O"


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


def test_scan_entry_signals_excludes_bases_below_min_days_to_expiry_floor():
    """
    MEJORA 2026-09-17 (ver config.LongFirstConfig.
    min_business_days_to_expiry_for_entry): con max_holding_business_days en
    None (sin limite, comportamiento real de produccion desde 2026-09-07),
    nada impedia entrar en un vencimiento demasiado CERCANO para tener
    mercado real. Con el piso configurado, una base cuyo days_business este
    por debajo se descarta ANTES de llegar al chequeo de dislocacion, sin
    importar que tan barata luzca.
    """
    cfg = _default_config(max_holding_business_days=None, min_business_days_to_expiry_for_entry=10)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC5000O", 5000, 0.45, spot, days_biz=3),    # barata, pero vence demasiado pronto
        _quote("GFGC5100O", 5100, 0.45, spot, days_biz=15),   # barata y con horizonte suficiente
        _quote("GFGC5300O", 5300, 0.58, spot, days_biz=3),
        _quote("GFGC6100O", 6100, 0.58, spot, days_biz=15),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert all(s.symbol != "GFGC5000O" for s in signals)
    assert any(s.symbol == "GFGC5100O" for s in signals)
    # GFGC5000O y GFGC5300O comparten days_biz=3 (por debajo del piso de 10)
    assert strategy.last_scan_diagnostics.blocked_by_min_days_to_expiry == 2


def test_scan_entry_signals_delta_band_filter_excludes_outside_band_when_enabled():
    """
    MEJORA 2026-09-17 (ver config.LongFirstConfig.enable_delta_band_filter):
    apagado por defecto (comportamiento identico a siempre); habilitado,
    filtra ADEMAS del moneyness por abs(delta) dentro de la banda
    configurada.
    """
    cfg = _default_config(enable_delta_band_filter=True, delta_band_min=0.40, delta_band_max=0.55)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC5100O", 5100, 0.45, spot, days_biz=3, greeks={"delta": 0.48, "gamma": 0.001, "vega": 2.0, "theta": -1.0}),
        _quote("GFGC5150O", 5150, 0.45, spot, days_biz=3, greeks={"delta": 0.20, "gamma": 0.001, "vega": 2.0, "theta": -1.0}),  # fuera de banda
        _quote("GFGC5250O", 5250, 0.58, spot, days_biz=3),  # sin Griegas -> descartada por este filtro
        _quote("GFGC6100O", 6100, 0.58, spot, days_biz=3, greeks={"delta": 0.50, "gamma": 0.001, "vega": 2.0, "theta": -1.0}),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5100O" for s in signals)
    assert all(s.symbol != "GFGC5150O" for s in signals)
    assert all(s.symbol != "GFGC5250O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_delta_band == 2


def test_scan_entry_signals_delta_band_filter_default_off_preserves_behavior():
    cfg = _default_config()  # enable_delta_band_filter no seteado -> False
    assert cfg.enable_delta_band_filter is False
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        # Delta MUY fuera de cualquier banda razonable, y sin Griegas en el
        # tercero - ninguna de las dos cosas debe filtrar nada con el flag apagado.
        _quote("GFGC5100O", 5100, 0.45, spot, days_biz=3, greeks={"delta": 0.05, "gamma": 0.001, "vega": 2.0, "theta": -1.0}),
        _quote("GFGC5300O", 5300, 0.58, spot, days_biz=3),
        _quote("GFGC6100O", 6100, 0.58, spot, days_biz=3),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5100O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_delta_band == 0


def test_scan_entry_signals_min_days_to_expiry_default_none_preserves_behavior():
    """Default None = sin piso: una base a solo 1 dia habil del vencimiento sigue calificando como antes."""
    cfg = _default_config()  # min_business_days_to_expiry_for_entry no seteado -> None
    assert cfg.min_business_days_to_expiry_for_entry is None
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC5000O", 5000, 0.45, spot, days_biz=1),
        _quote("GFGC5300O", 5300, 0.58, spot, days_biz=1),
        _quote("GFGC6100O", 6100, 0.58, spot, days_biz=1),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5000O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_min_days_to_expiry == 0


def test_scan_entry_signals_blocks_friday_entries_beyond_weekend_guard_when_enabled():
    """
    FIX 2026-09-29 (ver REPORT.md §4.0/§9.0, config.LongFirstConfig.
    weekend_theta_guard_block_new_entries): con el flag activado y `now`
    inyectado como un viernes, una entrada nueva sobre un vencimiento
    posterior a ese viernes queda bloqueada - esa posicion, si se abriera,
    tendria holding_business_days=0 y el weekend_theta_guard de salida la
    cerraria casi de inmediato (mismo patron real que produjo 194/199
    entradas de la muestra de Fase 0 cerradas en una mediana de 23s).
    """
    cfg = _default_config(weekend_theta_guard_block_new_entries=True)
    assert cfg.weekend_theta_guard_enabled is True  # precondicion: el guard de salida sigue activo
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC5000O", 5000, 0.45, spot, days_biz=3, expiry=date(2026, 10, 9)),
        _quote("GFGC5300O", 5300, 0.58, spot, days_biz=3, expiry=date(2026, 10, 9)),
        _quote("GFGC6100O", 6100, 0.58, spot, days_biz=3, expiry=date(2026, 10, 9)),
    ]
    surface = VolatilitySurface(quotes)
    friday = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)  # 2026-10-02 es viernes; vencimiento 2026-10-09 es posterior
    assert friday.weekday() == 4
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH", now=friday,
    )
    assert signals == []
    # Las 3 comparten vencimiento posterior al viernes: el guard bloquea la
    # entrada ANTES del chequeo de dislocacion (misma prioridad que el resto
    # de los filtros "estructurales" de arriba), sin importar si alguna
    # hubiera calificado como "barata".
    assert strategy.last_scan_diagnostics.blocked_by_weekend_entry_guard == 3


def test_scan_entry_signals_friday_guard_default_off_preserves_behavior():
    """Sin activar el flag (default False), un viernes se comporta exactamente igual que cualquier otro dia - sin cambios."""
    cfg = _default_config()  # weekend_theta_guard_block_new_entries no seteado -> False
    assert cfg.weekend_theta_guard_block_new_entries is False
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC5000O", 5000, 0.45, spot, days_biz=3, expiry=date(2026, 10, 9)),
        _quote("GFGC5300O", 5300, 0.58, spot, days_biz=3, expiry=date(2026, 10, 9)),
        _quote("GFGC6100O", 6100, 0.58, spot, days_biz=3, expiry=date(2026, 10, 9)),
    ]
    surface = VolatilitySurface(quotes)
    friday = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH", now=friday,
    )
    assert any(s.symbol == "GFGC5000O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_weekend_entry_guard == 0


def test_scan_entry_signals_friday_guard_ignores_non_friday_even_when_enabled():
    """Mismo escenario que arriba, pero `now` es un jueves - el flag activado no debe bloquear nada fuera de viernes."""
    cfg = _default_config(weekend_theta_guard_block_new_entries=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC5000O", 5000, 0.45, spot, days_biz=3, expiry=date(2026, 10, 9)),
        _quote("GFGC5300O", 5300, 0.58, spot, days_biz=3, expiry=date(2026, 10, 9)),
        _quote("GFGC6100O", 6100, 0.58, spot, days_biz=3, expiry=date(2026, 10, 9)),
    ]
    surface = VolatilitySurface(quotes)
    thursday = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
    assert thursday.weekday() == 3
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH", now=thursday,
    )
    assert any(s.symbol == "GFGC5000O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_weekend_entry_guard == 0


def test_scan_entry_signals_friday_guard_allows_entry_expiring_that_same_friday():
    """
    Si el vencimiento ES ese mismo viernes (q.expiry == now.date(), no
    posterior), el guard de salida nunca aplicaria (misma condicion
    `expiry > now.date()` que risk_manager.evaluate_position_exit) - el
    filtro de entrada debe espejar exactamente esa condicion y no bloquear.
    """
    cfg = _default_config(weekend_theta_guard_block_new_entries=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    friday_date = date(2026, 10, 2)
    quotes = [
        _quote("GFGC5000O", 5000, 0.45, spot, days_biz=0, expiry=friday_date),
        _quote("GFGC5300O", 5300, 0.58, spot, days_biz=0, expiry=friday_date),
        _quote("GFGC6100O", 6100, 0.58, spot, days_biz=0, expiry=friday_date),
    ]
    surface = VolatilitySurface(quotes)
    friday = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH", now=friday,
    )
    assert any(s.symbol == "GFGC5000O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_weekend_entry_guard == 0


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


def test_scan_entry_signals_funnel_log_disabled_by_default_leaves_candidate_funnel_empty():
    """
    Regresion de costo: LongFirstConfig.enable_signal_funnel_log default
    False (ver REPORT.md SS12.5 punto 5) - con el flag apagado,
    candidate_funnel debe quedar VACIO sin importar cuantas candidatas se
    evaluen, para que el costo adicional sea cero salvo activacion
    explicita.
    """
    cfg = _default_config()
    assert cfg.enable_signal_funnel_log is False
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC4900O", 4900, 0.60, spot, days_biz=3),
        _quote("GFGC5050O", 5050, 0.57, spot, days_biz=3),
        _quote("GFGC5200O", 5200, 0.45, spot, days_biz=3),   # calificaria
        _quote("GFGC5350O", 5350, 0.57, spot, days_biz=3),
        _quote("GFGC5500O", 5500, 0.60, spot, days_biz=3),
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5200O" for s in signals)  # comportamiento sin cambios
    assert strategy.last_scan_diagnostics.candidate_funnel == []


def test_scan_entry_signals_funnel_log_enabled_records_one_row_per_candidate_with_market_data():
    """
    Con el flag activado, candidate_funnel debe tener EXACTAMENTE una fila
    por cotizacion del universo (total_quotes), con blocked_at=None para la
    que califico y el nombre del filtro real para las que no - y los datos
    de mercado/spread de la fila deben venir de la cotizacion real, no
    fabricados.
    """
    cfg = _default_config(enable_signal_funnel_log=True, moneyness_band_pct=0.03)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGC4900O", 4900, 0.60, spot, days_biz=3, bid=90.0, ask=110.0),  # fuera de moneyness
        _quote("GFGC5050O", 5050, 0.57, spot, days_biz=3),
        _quote("GFGC5200O", 5200, 0.45, spot, days_biz=3, bid=98.0, ask=102.0),  # target: califica
        _quote("GFGC5350O", 5350, 0.57, spot, days_biz=3),
        _quote("GFGC5500O", 5500, 0.60, spot, days_biz=3, bid=90.0, ask=110.0),  # fuera de moneyness
    ]
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5200O" for s in signals)

    funnel = strategy.last_scan_diagnostics.candidate_funnel
    assert len(funnel) == len(quotes) == 5
    by_symbol = {r.symbol: r for r in funnel}

    qualified = by_symbol["GFGC5200O"]
    assert qualified.blocked_at is None
    assert qualified.bid == 98.0 and qualified.ask == 102.0
    assert qualified.spread_abs == 4.0
    assert abs(qualified.spread_relative - (4.0 / 100.0)) < 1e-9
    assert qualified.iv == 0.45
    assert qualified.dislocation_vol_points is not None

    out_of_band = by_symbol["GFGC4900O"]
    assert out_of_band.blocked_at == "moneyness"
    assert out_of_band.bid == 90.0 and out_of_band.ask == 110.0
    assert out_of_band.dislocation_vol_points is None  # nunca llego a ese chequeo


def test_scan_entry_signals_funnel_log_records_blocked_by_direction():
    """`blocked_at="direction"` para el option_type contrario a la tendencia, sin reversion de momentum."""
    cfg = _default_config(enable_signal_funnel_log=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    spot = 5200.0
    quotes = [
        _quote("GFGV5150O", 5150, 0.45, spot, days_biz=3, option_type=OptionType.PUT),
        _quote("GFGC4900O", 4900, 0.57, spot, days_biz=3),
        _quote("GFGC5500O", 5500, 0.57, spot, days_biz=3),
    ]
    surface = VolatilitySurface(quotes)
    strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    funnel = {r.symbol: r for r in strategy.last_scan_diagnostics.candidate_funnel}
    assert funnel["GFGV5150O"].blocked_at == "direction"
    assert funnel["GFGV5150O"].option_type == "put"


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
# strategy/weekly_asymmetric.py: scan_expensive_iv_spread_signals (MEJORA
# 2026-09-17, ver docstring de SpreadOpenSignal/config.LongFirstConfig.
# enable_expensive_iv_spread_entry)
# ---------------------------------------------------------------------------

def _expensive_iv_setup(long_bid=150.0, long_ask=160.0, wing_bid=90.0, wing_ask=100.0):
    """
    Smile sintetico PLANO (misma IV=0.45 en todos los strikes de relleno,
    ver test_scan_entry_signals_ranks_by_convexity_score_descending para el
    patron de smile con curvatura real) para que la curva ajustada quede
    ~0.45 en todo el rango; la base candidata queda 10 vol points POR
    ENCIMA (0.55) - "cara" en vez de "barata" - bien por encima del
    threshold default (3.0). El wing (mismo vencimiento, strike mas OTM)
    se cotiza con mid mas bajo que el largo por defecto, para que el
    spread resulte en debito neto positivo (parametrizable via los
    bid/ask para el test que necesita lo opuesto).
    """
    spot = 5200.0
    # Wing a 5500 vs. largo a 5200 = diferencia de 300 puntos de strike,
    # por encima del piso que exige el spread_wing_moneyness_pct default
    # de _default_config (0.05 * 5200 = 260) - a diferencia de
    # test_scan_spread_completion_signals_picks_further_otm_wing_for_bull_call_spread,
    # que si necesita overridear ese parametro porque su wing esta mas
    # cerca (5400).
    filler_strikes = [4700, 4900, 5000, 5300, 5600, 5700]
    filler = [_quote(f"GFGC{k}O", k, 0.45, spot, days_biz=3) for k in filler_strikes]
    long_quote = _quote(
        "GFGC5200O", 5200, 0.55, spot, days_biz=3, bid=long_bid, ask=long_ask,
    )
    wing_quote = _quote(
        "GFGC5500O", 5500, 0.45, spot, days_biz=3, bid=wing_bid, ask=wing_ask,
    )
    chain = OptionChain()
    chain.upsert_quote(long_quote)
    chain.upsert_quote(wing_quote)
    surface_quotes = filler + [long_quote]
    surface = VolatilitySurface(surface_quotes)
    volumes = {q.symbol: 1000.0 for q in surface_quotes + [wing_quote]}
    return surface, chain, volumes


def test_scan_expensive_iv_spread_signals_disabled_by_default():
    cfg = _default_config()  # enable_expensive_iv_spread_entry no seteado -> False
    assert cfg.enable_expensive_iv_spread_entry is False
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    surface, chain, volumes = _expensive_iv_setup()
    signals = strategy.scan_expensive_iv_spread_signals(surface, chain, volumes, trend="BULLISH")
    assert signals == []


def test_scan_expensive_iv_spread_signals_generates_debit_spread_when_enabled_and_expensive():
    cfg = _default_config(enable_expensive_iv_spread_entry=True, expensive_iv_spread_threshold_vol_points=3.0)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    surface, chain, volumes = _expensive_iv_setup()  # long mid=155, wing mid=95 -> debito neto=60
    signals = strategy.scan_expensive_iv_spread_signals(surface, chain, volumes, trend="BULLISH")
    assert len(signals) == 1
    signal = signals[0]
    assert signal.long_symbol == "GFGC5200O"
    assert signal.short_symbol == "GFGC5500O"
    assert signal.action == "open_debit_spread"
    assert signal.iv_dislocation_vol_points > cfg.expensive_iv_spread_threshold_vol_points
    assert signal.net_debit_premium == 60.0
    assert "Bull Call Spread" in signal.reason
    assert signal.trend_context == "BULLISH"


def test_scan_expensive_iv_spread_signals_neutral_trend_no_signal():
    """Igual que scan_spread_completion_signals: sin conviccion direccional no se asume el riesgo neto del spread."""
    cfg = _default_config(enable_expensive_iv_spread_entry=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    surface, chain, volumes = _expensive_iv_setup()
    signals = strategy.scan_expensive_iv_spread_signals(surface, chain, volumes, trend="NEUTRAL")
    assert signals == []


def test_scan_expensive_iv_spread_signals_bearish_trend_ignores_call_candidate():
    """Una base CALL cara no genera Bull Call Spread bajo BEARISH (direccion contraria)."""
    cfg = _default_config(enable_expensive_iv_spread_entry=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    surface, chain, volumes = _expensive_iv_setup()
    signals = strategy.scan_expensive_iv_spread_signals(surface, chain, volumes, trend="BEARISH")
    assert signals == []


def test_scan_expensive_iv_spread_signals_no_signal_when_net_debit_not_positive():
    """Si el wing quedara mas caro que la base larga (debito neto <= 0), el patron no aplica y no se genera señal."""
    cfg = _default_config(enable_expensive_iv_spread_entry=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    # wing (mid=165) mas caro que el largo (mid=155) -> net_debit = 155-165 = -10
    surface, chain, volumes = _expensive_iv_setup(wing_bid=160.0, wing_ask=170.0)
    signals = strategy.scan_expensive_iv_spread_signals(surface, chain, volumes, trend="BULLISH")
    assert signals == []


def test_scan_expensive_iv_spread_signals_below_threshold_no_signal():
    """Dislocacion positiva pero por debajo del umbral configurado no debe generar señal."""
    cfg = _default_config(enable_expensive_iv_spread_entry=True, expensive_iv_spread_threshold_vol_points=50.0)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    surface, chain, volumes = _expensive_iv_setup()  # dislocacion ~10 vol points, muy por debajo de 50
    signals = strategy.scan_expensive_iv_spread_signals(surface, chain, volumes, trend="BULLISH")
    assert signals == []


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
# build_exit_signals: salida por reversion de tendencia (MEJORA 2026-09-17,
# ver config.LongFirstConfig.enable_trend_reversal_exit y
# WeeklyAsymmetricStrategy._trend_has_reversed)
# ---------------------------------------------------------------------------

def test_build_exit_signals_trend_reversal_closes_call_position_when_trend_flips_bearish():
    cfg = _default_config(enable_trend_reversal_exit=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
        option_type="call", trend_at_entry="BULLISH",
    ))
    # Precio de la prima sin cambios (no dispara Stop Loss/Take Profit):
    # lo unico que debe gatillar el cierre aca es la reversion de tendencia.
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC5200O": 100.0}, now=now, trend="BEARISH",
    )
    assert len(signals) == 1
    assert signals[0].reason == "trend_reversal_exit"
    assert signals[0].action == "sell_to_close"
    assert signals[0].quantity == 5


def test_build_exit_signals_trend_reversal_closes_put_position_when_trend_flips_bullish():
    cfg = _default_config(enable_trend_reversal_exit=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFPV5200O", quantity=3, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
        option_type="put", trend_at_entry="BEARISH",
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFPV5200O": 100.0}, now=now, trend="BULLISH",
    )
    assert len(signals) == 1
    assert signals[0].reason == "trend_reversal_exit"


def test_build_exit_signals_trend_reversal_disabled_by_default_preserves_behavior():
    """enable_trend_reversal_exit no seteado -> False: una reversion completa no debe cerrar nada."""
    cfg = _default_config()
    assert cfg.enable_trend_reversal_exit is False
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
        option_type="call", trend_at_entry="BULLISH",
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC5200O": 100.0}, now=now, trend="BEARISH",
    )
    assert signals == []


def test_build_exit_signals_trend_reversal_neutral_current_trend_does_not_trigger():
    """Pasar de BULLISH a NEUTRAL es 'fading', no una reversion confirmada al extremo contrario - no debe cerrar."""
    cfg = _default_config(enable_trend_reversal_exit=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
        option_type="call", trend_at_entry="BULLISH",
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC5200O": 100.0}, now=now, trend="NEUTRAL",
    )
    assert signals == []


def test_build_exit_signals_trend_reversal_skipped_without_entry_metadata():
    """Una posicion sin option_type/trend_at_entry (legado, anterior a estos campos) nunca dispara esta salida."""
    cfg = _default_config(enable_trend_reversal_exit=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
        # sin option_type/trend_at_entry (default None)
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC5200O": 100.0}, now=now, trend="BEARISH",
    )
    assert signals == []


def test_build_exit_signals_trend_reversal_yields_priority_to_stop_loss():
    """Si Stop Loss YA dispara este mismo ciclo, la razon reportada debe seguir siendo 'stop_loss' (prioridad)."""
    cfg = _default_config(enable_trend_reversal_exit=True, stop_loss_pct=0.50)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
        option_type="call", trend_at_entry="BULLISH",
    ))
    # -60% de la prima: dispara Stop Loss (umbral 50%) Y la tendencia
    # tambien se reversó (BULLISH->BEARISH) en el mismo ciclo.
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC5200O": 40.0}, now=now, trend="BEARISH",
    )
    assert len(signals) == 1
    assert signals[0].reason == "stop_loss"


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


# ---------------------------------------------------------------------------
# strategy/weekly_asymmetric.py: build_exit_signals - Stop Loss escalonado,
# ventana minima de vega decay, y toma de ganancia parcial (MEJORAS
# 2026-09-04, ver seguimiento del analisis del export de trades del
# 01-04/09/2026)
# ---------------------------------------------------------------------------

def test_build_exit_signals_tiered_stop_loss_triggers_earlier_for_older_position():
    """
    Dos posiciones con el mismo PnL% (-40%): la que lleva 1 dia habil NO
    dispara (stage1, -50% fijo); la que lleva 2 dias habiles SI dispara
    (stage2, -35%) - confirma que build_exit_signals conecta cfg.
    enable_tiered_stop_loss/tiered_stop_loss_stage*_business_day/pct hasta
    evaluate_position_exit().
    """
    cfg = _default_config(
        enable_tiered_stop_loss=True,
        tiered_stop_loss_stage2_business_day=2, tiered_stop_loss_stage2_pct=0.35,
        tiered_stop_loss_stage3_business_day=4, tiered_stop_loss_stage3_pct=0.20,
        weekend_theta_guard_enabled=False,
    )
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    entry_time = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)  # lunes
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)         # miercoles: 2 dias habiles despues
    portfolio.add(Position(
        symbol="GFGC_NEW", quantity=5, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=2), expiry=date(2026, 9, 4),  # 0 dias habiles
    ))
    portfolio.add(Position(
        symbol="GFGC_OLD", quantity=5, multiplier=100.0,
        entry_price=100.0, entry_time=entry_time, expiry=date(2026, 9, 4),  # 2 dias habiles
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC_NEW": 60.0, "GFGC_OLD": 60.0}, now=now,
    )
    reasons_by_symbol = {s.symbol: s.reason for s in signals}
    assert "GFGC_NEW" not in reasons_by_symbol   # 0 dias: stage1, -50% fijo, -40% no alcanza
    assert reasons_by_symbol.get("GFGC_OLD") == "stop_loss"  # 2 dias: stage2, -35%, -40% si dispara


def test_build_exit_signals_vega_decay_min_holding_hours_blocks_early_close():
    """
    Misma compresion extrema de vega (20% del entry) en dos posiciones: la
    abierta hace 1h NO dispara (por debajo del minimo configurado); la
    abierta hace 4h SI dispara - confirma que build_exit_signals conecta
    cfg.vega_decay_min_holding_hours hasta evaluate_vega_decay_exit().
    """
    cfg = _default_config(vega_decay_exit_ratio=0.35, vega_decay_min_holding_hours=3.0)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC_YOUNG", quantity=5, multiplier=100.0,
        greeks_per_unit={"vega": 10.0, "gamma": 0.05, "delta": 0.5, "theta": -1.0},
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    portfolio.add(Position(
        symbol="GFGC_MATURE", quantity=5, multiplier=100.0,
        greeks_per_unit={"vega": 10.0, "gamma": 0.05, "delta": 0.5, "theta": -1.0},
        entry_price=100.0, entry_time=now - timedelta(hours=4), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(
        portfolio, current_prices={"GFGC_YOUNG": 105.0, "GFGC_MATURE": 105.0}, now=now,
        current_greeks={
            "GFGC_YOUNG": {"vega": 2.0, "gamma": 0.01, "delta": 0.8, "theta": -0.3},
            "GFGC_MATURE": {"vega": 2.0, "gamma": 0.01, "delta": 0.8, "theta": -0.3},
        },
    )
    reasons_by_symbol = {s.symbol: s.reason for s in signals}
    assert "GFGC_YOUNG" not in reasons_by_symbol
    assert reasons_by_symbol.get("GFGC_MATURE") == "vega_theta_decay"


def test_build_exit_signals_partial_profit_take_triggers_above_threshold_with_runner_left():
    cfg = _default_config(enable_partial_profit_take=True, partial_profit_trigger_pct=0.15, partial_profit_take_fraction=0.50)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=10, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 120.0}, now=now)  # +20%
    assert len(signals) == 1
    assert signals[0].reason == "partial_profit_take"
    assert signals[0].quantity == 5  # 50% de 10, deja un runner de 5
    assert signals[0].action == "sell_to_close"


def test_build_exit_signals_partial_profit_take_not_triggered_below_threshold():
    cfg = _default_config(enable_partial_profit_take=True, partial_profit_trigger_pct=0.15)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=10, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 108.0}, now=now)  # +8%
    assert signals == []


def test_build_exit_signals_partial_profit_take_not_retriggered_once_taken():
    """Position.partial_profit_taken=True (ya se tomo antes): no vuelve a generar señal aunque el PnL% siga alto."""
    cfg = _default_config(enable_partial_profit_take=True, partial_profit_trigger_pct=0.15)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=5, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
        partial_profit_taken=True,
    ))
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 130.0}, now=now)
    assert signals == []


def test_build_exit_signals_partial_profit_take_skipped_when_disabled():
    cfg = _default_config(enable_partial_profit_take=False)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=10, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 130.0}, now=now)
    assert signals == []


def test_build_exit_signals_partial_profit_take_skipped_with_single_contract():
    """Con quantity=1 no hay fraccion posible que deje un runner: no se toma ganancia parcial."""
    cfg = _default_config(enable_partial_profit_take=True, partial_profit_trigger_pct=0.15)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=1, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 130.0}, now=now)
    assert signals == []


def test_build_exit_signals_partial_profit_take_yields_to_full_close_reason():
    """Si Stop Loss/Take Profit/horizonte/vega decay ya dispararon, la toma de ganancia parcial ni se evalua."""
    cfg = _default_config(
        stop_loss_pct=0.50, enable_partial_profit_take=True, partial_profit_trigger_pct=0.15,
    )
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    portfolio = Portfolio()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    portfolio.add(Position(
        symbol="GFGC5200O", quantity=10, multiplier=100.0,
        entry_price=100.0, entry_time=now - timedelta(hours=1), expiry=date(2026, 9, 4),
    ))
    # +110%: dispara take_profit, muy por encima tambien del umbral de ganancia parcial (+15%)
    signals = strategy.build_exit_signals(portfolio, current_prices={"GFGC5200O": 210.0}, now=now)
    assert len(signals) == 1
    assert signals[0].reason == "take_profit"
    assert signals[0].quantity == 10  # cierre TOTAL, no la fraccion parcial


# ---------------------------------------------------------------------------
# run_bot.py: GgalOptionsBot._act_on_exit_signal - ejecucion de la señal de
# salida contra el portafolio (MEJORA 2026-09-04: rama nueva para
# "partial_profit_take" que descuenta cantidad en vez de vaciar la
# posicion - ver docstring de ese metodo).
# ---------------------------------------------------------------------------

def test_bot_act_on_exit_signal_full_close_zeroes_position():
    original_shadow = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True  # fill sincronico (ver order_gateway.py.send)
    try:
        bot = GgalOptionsBot()
        now = datetime.now(timezone.utc)
        bot.portfolio.add(Position(
            symbol="GFCLOSETEST", quantity=10, multiplier=100.0,
            entry_price=100.0, entry_time=now - timedelta(hours=1),
            expiry=date(2026, 9, 4), strategy_tag="weekly_asymmetric",
        ))
        quote = _quote("GFCLOSETEST", 5200, 0.45, 5200.0, days_biz=3, bid=39.0, ask=41.0)
        bot.option_chain.upsert_quote(quote)

        strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=_default_config(stop_loss_pct=0.50))
        signals = strategy.build_exit_signals(bot.portfolio, current_prices={"GFCLOSETEST": 40.0}, now=now)
        assert len(signals) == 1 and signals[0].reason == "stop_loss"

        bot._act_on_exit_signal(signals[0], spot=5200.0)

        pos = next(p for p in bot.portfolio.positions if p.symbol == "GFCLOSETEST")
        assert pos.quantity == 0.0
        assert pos.partial_profit_taken is False
    finally:
        SETTINGS.shadow.enabled = original_shadow


def test_bot_act_on_exit_signal_partial_profit_take_reduces_quantity_and_sets_flag():
    original_shadow = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        now = datetime.now(timezone.utc)
        bot.portfolio.add(Position(
            symbol="GFPARTIALTEST", quantity=10, multiplier=100.0,
            entry_price=100.0, entry_time=now - timedelta(hours=1),
            expiry=date(2026, 9, 4), strategy_tag="weekly_asymmetric",
        ))
        quote = _quote("GFPARTIALTEST", 5200, 0.45, 5200.0, days_biz=3, bid=119.0, ask=121.0)
        bot.option_chain.upsert_quote(quote)

        cfg = _default_config(enable_partial_profit_take=True, partial_profit_trigger_pct=0.15, partial_profit_take_fraction=0.50)
        strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
        signals = strategy.build_exit_signals(bot.portfolio, current_prices={"GFPARTIALTEST": 120.0}, now=now)
        assert len(signals) == 1 and signals[0].reason == "partial_profit_take" and signals[0].quantity == 5

        bot._act_on_exit_signal(signals[0], spot=5200.0)

        pos = next(p for p in bot.portfolio.positions if p.symbol == "GFPARTIALTEST")
        assert pos.quantity == 5.0
        assert pos.partial_profit_taken is True
    finally:
        SETTINGS.shadow.enabled = original_shadow


# ---------------------------------------------------------------------------
# MEJORAS 2026-09-28: z-score adaptativo, costo de ejecucion, blackout de
# earnings, override ADR/CCL (todas SE SUMAN a los filtros existentes,
# apagadas por defecto en _default_config() salvo que el test las active
# explicitamente - ver docstrings en config.py y strategy/weekly_asymmetric.py).
# ---------------------------------------------------------------------------

def test_scan_entry_signals_zscore_filter_blocks_without_enough_history():
    """Con el filtro activado, una base sin historia de z-score (None) debe bloquearse - la
    ausencia de informacion nunca abre riesgo nuevo (mismo criterio que el resto de los filtros)."""
    cfg = _default_config(enable_zscore_filter=True, zscore_threshold=1.5)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
        dislocation_zscore=None,
    )
    assert not any(s.symbol == "GFGC5150O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_zscore > 0


def test_scan_entry_signals_zscore_filter_blocks_when_not_extreme_enough():
    cfg = _default_config(enable_zscore_filter=True, zscore_threshold=1.5)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    # z=-1.0 no alcanza el umbral configurado (-1.5): debe bloquear.
    zscores = {q.symbol: -1.0 for q in quotes}
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
        dislocation_zscore=zscores,
    )
    assert not any(s.symbol == "GFGC5150O" for s in signals)


def test_scan_entry_signals_zscore_filter_allows_extreme_zscore():
    cfg = _default_config(enable_zscore_filter=True, zscore_threshold=1.5)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    # z=-2.0 supera el umbral (mas negativo que -1.5): debe permitir la señal
    # para la base que ya califica por dislocacion absoluta (GFGC5150O).
    zscores = {q.symbol: -2.0 for q in quotes}
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
        dislocation_zscore=zscores,
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)


def test_scan_entry_signals_zscore_filter_disabled_ignores_missing_history():
    """Con el flag apagado (default), no pasar dislocation_zscore no debe bloquear nada -
    comportamiento identico al de antes de esta mejora."""
    cfg = _default_config(enable_zscore_filter=False)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_zscore == 0


def test_scan_entry_signals_execution_cost_filter_blocks_thin_wide_book():
    """Un libro con spread ancho y ask_size chico (costo estimado alto) debe bloquear
    la señal aunque la dislocacion de IV sea real - filtro de calidad de ejecucion."""
    cfg = _default_config(enable_execution_cost_filter=True, execution_cost_max_pct=0.08)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    for q in quotes:
        if q.symbol == "GFGC5150O":
            # mid=50, spread=40 -> half_spread_pct=0.40; muy por encima de 0.08.
            q.book.bid = 30.0
            q.book.ask = 70.0
            q.book.ask_size = 5.0
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert not any(s.symbol == "GFGC5150O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_execution_cost > 0


def test_scan_entry_signals_execution_cost_filter_allows_tight_book():
    cfg = _default_config(enable_execution_cost_filter=True, execution_cost_max_pct=0.08)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    for q in quotes:
        if q.symbol == "GFGC5150O":
            # mid=100, spread=10 -> half_spread_pct=0.05; ask_size grande -> impacto despreciable.
            # bid_size se sube igual que ask_size para no disparar el filtro OBI (no es lo que este test aisla).
            q.book.bid = 95.0
            q.book.ask = 105.0
            q.book.bid_size = 500.0
            q.book.ask_size = 500.0
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)


def test_scan_entry_signals_execution_cost_filter_disabled_preserves_behavior():
    cfg = _default_config(enable_execution_cost_filter=False)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    for q in quotes:
        if q.symbol == "GFGC5150O":
            q.book.bid = 30.0
            q.book.ask = 70.0
            q.book.ask_size = 5.0
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)
    assert strategy.last_scan_diagnostics.blocked_by_execution_cost == 0


def test_scan_entry_signals_earnings_blackout_blocks_everything_when_enabled():
    cfg = _default_config(enable_earnings_blackout=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
        earnings_blackout=True,
    )
    assert signals == []
    diag = strategy.last_scan_diagnostics
    assert diag.blocked_by_earnings_blackout == len(quotes)


def test_scan_entry_signals_earnings_blackout_noop_outside_window():
    cfg = _default_config(enable_earnings_blackout=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
        earnings_blackout=False,
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)


def test_scan_entry_signals_earnings_blackout_disabled_ignores_flag_even_if_true():
    """Con el flag de config apagado, aunque el llamador pase earnings_blackout=True
    (ej. bug en run_bot.py calculando la fecha), no debe bloquear nada - el gate es
    SIEMPRE `earnings_blackout and cfg.enable_earnings_blackout`."""
    cfg = _default_config(enable_earnings_blackout=False)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
        earnings_blackout=True,
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)


def test_scan_entry_signals_adr_ccl_tightens_to_extreme_when_contradicting_trend():
    """
    ADR/CCL BEARISH mientras `trend` (tecnico 1D) lee BULLISH: para el CALL
    (soportado por `trend`) debe exigirse el umbral EXTREMO en vez del
    normal, nunca bloquear de plano (mismo patron que Momentum Shift).
    GFGC5150O tiene una dislocacion de ~-3.9 vol pts: pasa el umbral normal
    (3.0) pero no el extremo (6.0) -> debe dejar de calificar.
    """
    cfg = _default_config(enable_adr_ccl_filter=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals_without_adr = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5150O" for s in signals_without_adr)  # baseline: califica bajo umbral normal

    signals_with_adr = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
        adr_ccl_trend="BEARISH",
    )
    assert not any(s.symbol == "GFGC5150O" for s in signals_with_adr)


def test_scan_entry_signals_adr_ccl_no_effect_when_agreeing_with_trend():
    cfg = _default_config(enable_adr_ccl_filter=True)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
        adr_ccl_trend="BULLISH",
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)


def test_scan_entry_signals_adr_ccl_disabled_ignores_provided_trend():
    cfg = _default_config(enable_adr_ccl_filter=False)
    strategy = WeeklyAsymmetricStrategy(_lenient_risk_manager(), config=cfg)
    quotes = _bullish_smile_quotes()
    surface = VolatilitySurface(quotes)
    signals = strategy.scan_entry_signals(
        surface, recent_volumes={q.symbol: 1000.0 for q in quotes}, trend="BULLISH",
        adr_ccl_trend="BEARISH",
    )
    assert any(s.symbol == "GFGC5150O" for s in signals)


# ---------------------------------------------------------------------------
# risk/position_sizer.py: sizing por conviccion (MEJORA 2026-09-28, ver
# config.LongFirstConfig.enable_conviction_sizing).
# ---------------------------------------------------------------------------

def test_conviction_multiplier_disabled_is_always_one():
    cfg = _default_config(enable_conviction_sizing=False)
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    sizer.conviction_sizing_enabled = getattr(cfg, "enable_conviction_sizing", False)
    assert sizer.conviction_multiplier_for(9.0) == 1.0
    assert sizer.conviction_multiplier_for(None) == 1.0


def test_conviction_multiplier_at_reference_is_one():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    sizer.conviction_sizing_enabled = True
    sizer.conviction_sizing_reference_vol_points = 3.0
    sizer.conviction_sizing_min_multiplier = 0.5
    sizer.conviction_sizing_max_multiplier = 1.5
    assert sizer.conviction_multiplier_for(3.0) == 1.0
    assert sizer.conviction_multiplier_for(-3.0) == 1.0  # abs()


def test_conviction_multiplier_scales_up_and_clamps_to_max():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    sizer.conviction_sizing_enabled = True
    sizer.conviction_sizing_reference_vol_points = 3.0
    sizer.conviction_sizing_min_multiplier = 0.5
    sizer.conviction_sizing_max_multiplier = 1.5
    # 6.0 / 3.0 = 2.0x crudo, pero el techo configurado es 1.5x.
    assert sizer.conviction_multiplier_for(6.0) == 1.5


def test_conviction_multiplier_scales_down_and_clamps_to_min():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    sizer.conviction_sizing_enabled = True
    sizer.conviction_sizing_reference_vol_points = 3.0
    sizer.conviction_sizing_min_multiplier = 0.5
    sizer.conviction_sizing_max_multiplier = 1.5
    # 1.5 / 3.0 = 0.5x crudo, coincide exactamente con el piso configurado.
    assert sizer.conviction_multiplier_for(1.5) == 0.5


def test_conviction_multiplier_without_valid_reference_is_one():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    sizer.conviction_sizing_enabled = True
    sizer.conviction_sizing_reference_vol_points = None
    assert sizer.conviction_multiplier_for(9.0) == 1.0


def test_compute_contracts_applies_conviction_multiplier_to_allocated_capital():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    # Baseline (multiplicador 1.0): capital_asignado=200,000; prima=350 -> costo/contrato=35,000 -> 5 contratos.
    baseline = sizer.compute_contracts(premium_price=350.0, conviction_multiplier=1.0)
    assert baseline.contracts == 5
    assert baseline.capital_allocated_ars == 200_000.0

    # Con 1.5x: capital_asignado=300,000 -> 300,000/35,000 = 8.57 -> floor = 8.
    boosted = sizer.compute_contracts(premium_price=350.0, conviction_multiplier=1.5)
    assert boosted.contracts == 8
    assert boosted.capital_allocated_ars == 300_000.0


def test_compute_contracts_rejects_non_positive_conviction_multiplier():
    sizer = PositionSizer(max_capital_ars=1_000_000.0, max_risk_pct_per_trade=0.20, option_multiplier=100.0)
    result = sizer.compute_contracts(premium_price=350.0, conviction_multiplier=0.0)
    assert result.contracts == 0
    assert result.rejected_reason == "conviction_multiplier_invalido"


ALL_TESTS = [
    test_position_sizer_applies_floor_division_formula,
    test_position_sizer_rejects_when_capital_insufficient_for_one_contract,
    test_position_sizer_never_exceeds_max_capital_ceiling,
    test_position_sizer_rejects_invalid_premium,
    test_evaluate_position_exit_triggers_stop_loss,
    test_evaluate_position_exit_triggers_take_profit,
    test_evaluate_position_exit_triggers_weekly_horizon_expired,
    test_evaluate_position_exit_horizon_disabled_when_max_holding_business_days_is_none,
    test_evaluate_position_exit_triggers_weekend_theta_guard_on_friday,
    test_evaluate_position_exit_weekend_guard_skipped_if_expires_same_friday,
    test_evaluate_position_exit_weekend_guard_still_fires_when_cap_not_configured,
    test_evaluate_position_exit_weekend_guard_exempts_position_past_configured_cap,
    test_evaluate_position_exit_weekend_guard_still_fires_below_configured_cap,
    test_evaluate_position_exit_returns_none_within_all_bands,
    test_evaluate_position_exit_handles_missing_current_price,
    test_evaluate_position_exit_horizon_expired_fires_even_without_current_price,
    test_evaluate_position_exit_weekend_guard_fires_even_without_current_price,
    test_evaluate_position_exit_still_returns_none_without_price_when_no_calendar_condition_met,
    test_evaluate_vega_decay_exit_triggers_below_threshold,
    test_evaluate_vega_decay_exit_does_not_trigger_above_threshold,
    test_evaluate_vega_decay_exit_boundary_is_inclusive,
    test_evaluate_vega_decay_exit_handles_missing_values,
    test_evaluate_vega_decay_exit_sign_agnostic,
    test_evaluate_vega_decay_exit_blocked_before_min_holding_hours,
    test_evaluate_vega_decay_exit_allowed_after_min_holding_hours,
    test_evaluate_vega_decay_exit_min_holding_hours_ignored_without_time_args,
    test_evaluate_position_exit_tiered_stop_loss_stage1_uses_fixed_pct,
    test_evaluate_position_exit_tiered_stop_loss_stage2_narrows_threshold,
    test_evaluate_position_exit_tiered_stop_loss_stage3_narrows_further,
    test_evaluate_position_exit_tiered_stop_loss_disabled_preserves_fixed_pct,
    test_evaluate_partial_profit_take_triggers_above_threshold,
    test_evaluate_partial_profit_take_boundary_is_inclusive,
    test_evaluate_partial_profit_take_not_triggered_below_threshold,
    test_evaluate_partial_profit_take_skipped_if_already_taken,
    test_evaluate_partial_profit_take_handles_missing_values,
    test_scan_entry_signals_emits_buy_signal_for_cheap_base_in_band_and_horizon,
    test_scan_entry_signals_never_emits_signal_for_expensive_base,
    test_scan_entry_signals_excludes_bases_beyond_weekly_horizon,
    test_scan_entry_signals_includes_bases_beyond_horizon_when_limit_disabled,
    test_scan_entry_signals_excludes_bases_outside_moneyness_band,
    test_scan_entry_signals_excludes_bases_below_min_days_to_expiry_floor,
    test_scan_entry_signals_delta_band_filter_excludes_outside_band_when_enabled,
    test_scan_entry_signals_delta_band_filter_default_off_preserves_behavior,
    test_scan_entry_signals_min_days_to_expiry_default_none_preserves_behavior,
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
    test_scan_expensive_iv_spread_signals_disabled_by_default,
    test_scan_expensive_iv_spread_signals_generates_debit_spread_when_enabled_and_expensive,
    test_scan_expensive_iv_spread_signals_neutral_trend_no_signal,
    test_scan_expensive_iv_spread_signals_bearish_trend_ignores_call_candidate,
    test_scan_expensive_iv_spread_signals_no_signal_when_net_debit_not_positive,
    test_scan_expensive_iv_spread_signals_below_threshold_no_signal,
    test_build_exit_signals_skips_positions_missing_entry_metadata,
    test_build_exit_signals_ignores_non_long_positions,
    test_build_exit_signals_produces_stop_loss_signal,
    test_build_exit_signals_trend_reversal_closes_call_position_when_trend_flips_bearish,
    test_build_exit_signals_trend_reversal_closes_put_position_when_trend_flips_bullish,
    test_build_exit_signals_trend_reversal_disabled_by_default_preserves_behavior,
    test_build_exit_signals_trend_reversal_neutral_current_trend_does_not_trigger,
    test_build_exit_signals_trend_reversal_skipped_without_entry_metadata,
    test_build_exit_signals_trend_reversal_yields_priority_to_stop_loss,
    test_scan_entry_signals_obi_filter_blocks_extreme_sell_side_imbalance,
    test_scan_entry_signals_obi_filter_allows_normal_imbalance,
    test_scan_entry_signals_obi_filter_disabled_ignores_imbalance,
    test_build_exit_signals_vega_decay_triggers_when_convexity_exhausted,
    test_build_exit_signals_vega_decay_does_not_trigger_above_threshold,
    test_build_exit_signals_vega_decay_skipped_without_current_greeks,
    test_build_exit_signals_vega_decay_disabled_by_config,
    test_build_exit_signals_stop_loss_takes_priority_over_vega_decay,
    test_build_exit_signals_tiered_stop_loss_triggers_earlier_for_older_position,
    test_build_exit_signals_vega_decay_min_holding_hours_blocks_early_close,
    test_build_exit_signals_partial_profit_take_triggers_above_threshold_with_runner_left,
    test_build_exit_signals_partial_profit_take_not_triggered_below_threshold,
    test_build_exit_signals_partial_profit_take_not_retriggered_once_taken,
    test_build_exit_signals_partial_profit_take_skipped_when_disabled,
    test_build_exit_signals_partial_profit_take_skipped_with_single_contract,
    test_build_exit_signals_partial_profit_take_yields_to_full_close_reason,
    test_bot_act_on_exit_signal_full_close_zeroes_position,
    test_bot_act_on_exit_signal_partial_profit_take_reduces_quantity_and_sets_flag,
    test_scan_entry_signals_zscore_filter_blocks_without_enough_history,
    test_scan_entry_signals_zscore_filter_blocks_when_not_extreme_enough,
    test_scan_entry_signals_zscore_filter_allows_extreme_zscore,
    test_scan_entry_signals_zscore_filter_disabled_ignores_missing_history,
    test_scan_entry_signals_execution_cost_filter_blocks_thin_wide_book,
    test_scan_entry_signals_execution_cost_filter_allows_tight_book,
    test_scan_entry_signals_execution_cost_filter_disabled_preserves_behavior,
    test_scan_entry_signals_earnings_blackout_blocks_everything_when_enabled,
    test_scan_entry_signals_earnings_blackout_noop_outside_window,
    test_scan_entry_signals_earnings_blackout_disabled_ignores_flag_even_if_true,
    test_scan_entry_signals_adr_ccl_tightens_to_extreme_when_contradicting_trend,
    test_scan_entry_signals_adr_ccl_no_effect_when_agreeing_with_trend,
    test_scan_entry_signals_adr_ccl_disabled_ignores_provided_trend,
    test_conviction_multiplier_disabled_is_always_one,
    test_conviction_multiplier_at_reference_is_one,
    test_conviction_multiplier_scales_up_and_clamps_to_max,
    test_conviction_multiplier_scales_down_and_clamps_to_min,
    test_conviction_multiplier_without_valid_reference_is_one,
    test_compute_contracts_applies_conviction_multiplier_to_allocated_capital,
    test_compute_contracts_rejects_non_positive_conviction_multiplier,
    test_scan_entry_signals_funnel_log_disabled_by_default_leaves_candidate_funnel_empty,
    test_scan_entry_signals_funnel_log_enabled_records_one_row_per_candidate_with_market_data,
    test_scan_entry_signals_funnel_log_records_blocked_by_direction,
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
