"""
test_vol_arbitrage_exit_management.py
=======================================
Tests de regresion para un bug real encontrado analizando el export de
trades 2026-09-17T17-13_export.csv y confirmado por log de produccion
(2026-09-16 10:47:18 UTC: "Salida GFGC8000OC [reason=stop_loss]...
requested_qty=58.00"):

    _run_vol_arbitrage_cycle() (run_bot.py) UNICAMENTE escaneaba señales de
    entrada - nunca evaluaba ninguna condicion de salida sobre posiciones ya
    abiertas. VolatilityArbitrageStrategy.scan_for_signals() SI emite una
    señal "sell" cuando la IV se encarece, pero _act_on_signal() la
    descartaba sin mas en cuanto ya existia una posicion en esa base (Guarda
    2) - el propio docstring de _act_on_signal ya admitia esto como TODO.
    Ademas, esas posiciones se guardaban con Position.strategy_tag=None, que
    "por convencion" se trata como "weekly_asymmetric" en el resto del bot -
    un estado ambiguo real.

    Consecuencia real (shadow): GFGC8000OC (6 lotes, 58 contratos) quedo sin
    ningun stop ni horizonte durante ~13 dias mientras la prima colapsaba de
    ~170-186 a 55.24 (-66% a -70%). Perdida: -$681.053.

FIX (ver config.VolArbitrageConfig y run_bot.py._check_vol_arbitrage_exits):
    1) Las posiciones de vol_arbitrage ahora se tagean explicitamente con
       strategy_tag="vol_arbitrage" (ya no quedan en None).
    2) _run_vol_arbitrage_cycle() ahora evalua salidas (Stop Loss/Take
       Profit/horizonte/guardia de fin de semana, via RiskManager.
       evaluate_position_exit(), la misma fuente de verdad que usa
       weekly_asymmetric) ANTES de escanear entradas nuevas en cada ciclo,
       gateado por VolArbitrageConfig.enabled (default True: corrige un bug
       de riesgo real, no un cambio de comportamiento opcional).

Correr con:
    python -m ggal_bot.validation.test_vol_arbitrage_exit_management
"""
from __future__ import annotations

import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from datetime import date, datetime, timedelta, timezone

# Debe importarse ANTES que ggal_bot.execution.order_gateway/run_bot - ver
# test_execution_pipeline.py.
from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OptionQuote, OrderBookSnapshot
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Position
from ggal_bot.strategy.vol_arbitrage import TradeSignal
import run_bot as run_bot_module
from run_bot import GgalOptionsBot


class _FixedNowDatetime(datetime):
    """
    Congela run_bot.datetime.now() a un miercoles fijo (ver
    test_check_vol_arbitrage_exits_leaves_healthy_position_untouched) -
    NO toca production code, solo reemplaza la referencia `datetime` en el
    namespace de run_bot.py durante un test puntual.

    BUG DE AISLAMIENTO REAL (hallado 2026-10-02, primer viernes calendario
    en que corrio esta suite - no relacionado con ningun item de Tarea #27):
    _check_vol_arbitrage_exits() usa `datetime.now(timezone.utc)` real (sin
    forma de inyectar "now" desde afuera, a diferencia de
    RiskManager.evaluate_position_exit(), que SI recibe `now` como
    parametro explicito). RiskManager.evaluate_position_exit() fuerza un
    cierre via "weekend_theta_guard" cualquier viernes (now.weekday()==4,
    ver risk_manager.py) sin importar el precio - comportamiento real,
    intencional y documentado del bot (protege contra decay de fin de
    semana sin rueda para reaccionar). Este test en particular espera "la
    posicion sana queda intacta" evaluando SOLO movimiento de precio
    (+2.8%, no dispara stop/take profit) - una premisa que dejo de
    cumplirse automaticamente el primer viernes real en que se ejecuto,
    sin que nada del codigo ni de Tarea #27 haya cambiado. Se fija "now" a
    un miercoles para que el resultado sea deterministico el 365/366 dias
    del año, no solo de lunes a jueves.
    """
    _FIXED = datetime(2026, 9, 16, 15, 0, 0, tzinfo=timezone.utc)  # miercoles

    @classmethod
    def now(cls, tz=None):
        return cls._FIXED.astimezone(tz) if tz is not None else cls._FIXED


def _make_vol_arbitrage_bot() -> GgalOptionsBot:
    """
    NOTA: a diferencia de test_signal_shutdown.py::_make_bot(), esta NO
    restaura SETTINGS.shadow.enabled antes de retornar - OrderGateway.send()
    lo relee dinamicamente en cada submit() (no queda "congelado" en el
    bot al construirlo), asi que restaurarlo aca adentro rompe el fill
    sincronico que estos tests necesitan durante toda su ejecucion. Cada
    test es responsable de restaurar shadow.enabled/strategy.active en su
    propio try/finally (mismo patron que test_execution_pipeline.py::
    test_act_on_signal_does_not_reenter_same_symbol_across_cycles_in_shadow_mode).
    """
    SETTINGS.strategy.active = "vol_arbitrage"
    SETTINGS.shadow.enabled = True
    return GgalOptionsBot()


def _upsert_quote(bot, symbol, mid, expiry=date(2026, 12, 18)):
    half_spread = max(mid * 0.02, 0.01)
    book = OrderBookSnapshot(symbol, bid=mid - half_spread, ask=mid + half_spread, bid_size=50, ask_size=50)
    q = OptionQuote(symbol, strike=8000.0, expiry=expiry, option_type=OptionType.CALL,
                     book=book, days_calendar=90, days_business=60)
    q.greeks = {"delta": 0.3, "gamma": 0.001, "vega": 2.0, "theta": -1.0, "rho": 0.1, "price": mid}
    bot.option_chain.upsert_quote(q)


def test_act_on_signal_tags_new_positions_as_vol_arbitrage():
    """
    FIX 2026-09-17: antes Position.strategy_tag quedaba en None (tratado
    "por convencion" como weekly_asymmetric en el resto del bot) - ahora se
    marca explicitamente, igual que ya se hace con "scalping".
    """
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    try:
        bot = _make_vol_arbitrage_bot()
        _upsert_quote(bot, "GFGC8000OC", mid=180.0)
        signal = TradeSignal(symbol="GFGC8000OC", action="buy", reason="test", iv_dislocation_vol_points=-5.0)

        bot._act_on_signal(signal, spot=7000.0)

        matching = [p for p in bot.portfolio.positions if p.symbol == "GFGC8000OC"]
        assert len(matching) == 1
        assert matching[0].strategy_tag == "vol_arbitrage"
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow


def test_check_vol_arbitrage_exits_closes_position_on_stop_loss():
    """Reproduce el incidente real: prima cae de 180 a 55.24 (-69%) -> debe cerrarse por stop_loss."""
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    try:
        bot = _make_vol_arbitrage_bot()
        bot.portfolio.add(Position(
            symbol="GFGC8000OC", quantity=58, multiplier=100.0,
            greeks_per_unit={"delta": 0.3, "gamma": 0.001, "vega": 2.0, "theta": -1.0},
            expiry=date(2026, 12, 18),
            entry_price=180.0, entry_time=datetime.now(timezone.utc) - timedelta(days=13),
            strategy_tag="vol_arbitrage",
        ))
        _upsert_quote(bot, "GFGC8000OC", mid=55.24)

        bot._check_vol_arbitrage_exits(spot=7000.0)

        remaining = sum(p.quantity for p in bot.portfolio.positions if p.symbol == "GFGC8000OC")
        assert remaining == 0, "la posicion debia cerrarse por completo (stop_loss)"
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow


def test_check_vol_arbitrage_exits_leaves_healthy_position_untouched():
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_datetime = run_bot_module.datetime
    run_bot_module.datetime = _FixedNowDatetime  # ver docstring de _FixedNowDatetime
    try:
        bot = _make_vol_arbitrage_bot()
        bot.portfolio.add(Position(
            symbol="GFGC7000OC", quantity=3, multiplier=100.0,
            greeks_per_unit={"delta": 0.4, "gamma": 0.001, "vega": 2.0, "theta": -1.0},
            expiry=date(2026, 12, 18),
            entry_price=180.0, entry_time=_FixedNowDatetime.now() - timedelta(days=1),
            strategy_tag="vol_arbitrage",
        ))
        _upsert_quote(bot, "GFGC7000OC", mid=185.0)  # +2.8%, no dispara nada

        bot._check_vol_arbitrage_exits(spot=7000.0)

        remaining = sum(p.quantity for p in bot.portfolio.positions if p.symbol == "GFGC7000OC")
        assert remaining == 3
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        run_bot_module.datetime = original_datetime


def test_check_vol_arbitrage_exits_noop_when_disabled():
    """VolArbitrageConfig.enabled=False debe restaurar el comportamiento de siempre (sin gestion de salida)."""
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_enabled = SETTINGS.vol_arbitrage.enabled
    try:
        bot = _make_vol_arbitrage_bot()
        bot.portfolio.add(Position(
            symbol="GFGC8000OC", quantity=58, multiplier=100.0,
            greeks_per_unit={"delta": 0.3, "gamma": 0.001, "vega": 2.0, "theta": -1.0},
            expiry=date(2026, 12, 18),
            entry_price=180.0, entry_time=datetime.now(timezone.utc) - timedelta(days=13),
            strategy_tag="vol_arbitrage",
        ))
        _upsert_quote(bot, "GFGC8000OC", mid=55.24)

        SETTINGS.vol_arbitrage.enabled = False
        bot._check_vol_arbitrage_exits(spot=7000.0)

        remaining = sum(p.quantity for p in bot.portfolio.positions if p.symbol == "GFGC8000OC")
        assert remaining == 58, "con enabled=False no debia tocarse la posicion"
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.vol_arbitrage.enabled = original_enabled


def test_check_vol_arbitrage_exits_stop_loss_sets_reentry_cooldown():
    """
    MEJORA 2026-09-17 (ver VolArbitrageConfig.reentry_cooldown_seconds):
    tras un cierre por stop_loss, con el cooldown configurado, la base debe
    quedar registrada en GgalOptionsBot._vol_arbitrage_reentry_cooldown_until
    con un timestamp futuro.
    """
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_cooldown = SETTINGS.vol_arbitrage.reentry_cooldown_seconds
    try:
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = 60.0
        bot = _make_vol_arbitrage_bot()
        bot.portfolio.add(Position(
            symbol="GFGC8000OC", quantity=58, multiplier=100.0,
            greeks_per_unit={"delta": 0.3, "gamma": 0.001, "vega": 2.0, "theta": -1.0},
            expiry=date(2026, 12, 18),
            entry_price=180.0, entry_time=datetime.now(timezone.utc) - timedelta(days=13),
            strategy_tag="vol_arbitrage",
        ))
        _upsert_quote(bot, "GFGC8000OC", mid=55.24)

        before = time.time()
        bot._check_vol_arbitrage_exits(spot=7000.0)

        cooldown_until = bot._vol_arbitrage_reentry_cooldown_until.get("GFGC8000OC")
        assert cooldown_until is not None
        assert cooldown_until > before
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = original_cooldown


def test_check_vol_arbitrage_exits_no_cooldown_recorded_by_default():
    """reentry_cooldown_seconds=None (default) -> ningun cooldown se registra, comportamiento previo intacto."""
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_cooldown = SETTINGS.vol_arbitrage.reentry_cooldown_seconds
    try:
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = None
        bot = _make_vol_arbitrage_bot()
        bot.portfolio.add(Position(
            symbol="GFGC8000OC", quantity=58, multiplier=100.0,
            greeks_per_unit={"delta": 0.3, "gamma": 0.001, "vega": 2.0, "theta": -1.0},
            expiry=date(2026, 12, 18),
            entry_price=180.0, entry_time=datetime.now(timezone.utc) - timedelta(days=13),
            strategy_tag="vol_arbitrage",
        ))
        _upsert_quote(bot, "GFGC8000OC", mid=55.24)

        bot._check_vol_arbitrage_exits(spot=7000.0)

        assert "GFGC8000OC" not in bot._vol_arbitrage_reentry_cooldown_until
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = original_cooldown


def test_check_vol_arbitrage_exits_take_profit_does_not_set_cooldown():
    """El cooldown es UNICAMENTE para stop_loss (ver docstring de config.VolArbitrageConfig.reentry_cooldown_seconds)."""
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_cooldown = SETTINGS.vol_arbitrage.reentry_cooldown_seconds
    try:
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = 60.0
        bot = _make_vol_arbitrage_bot()
        bot.portfolio.add(Position(
            symbol="GFGC8000OC", quantity=5, multiplier=100.0,
            greeks_per_unit={"delta": 0.3, "gamma": 0.001, "vega": 2.0, "theta": -1.0},
            expiry=date(2026, 12, 18),
            entry_price=100.0, entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
            strategy_tag="vol_arbitrage",
        ))
        _upsert_quote(bot, "GFGC8000OC", mid=210.0)  # +110%, dispara take_profit (umbral 100%)

        bot._check_vol_arbitrage_exits(spot=7000.0)

        remaining = sum(p.quantity for p in bot.portfolio.positions if p.symbol == "GFGC8000OC")
        assert remaining == 0, "la posicion debia cerrarse por completo (take_profit)"
        assert "GFGC8000OC" not in bot._vol_arbitrage_reentry_cooldown_until
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = original_cooldown


def test_act_on_signal_blocked_during_reentry_cooldown():
    """Guarda 0 (MEJORA 2026-09-17): una base en cooldown no debe reabrirse aunque no haya posicion ni orden en vigilancia."""
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_cooldown = SETTINGS.vol_arbitrage.reentry_cooldown_seconds
    try:
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = 60.0
        bot = _make_vol_arbitrage_bot()
        _upsert_quote(bot, "GFGC8000OC", mid=180.0)
        bot._vol_arbitrage_reentry_cooldown_until["GFGC8000OC"] = time.time() + 60.0

        signal = TradeSignal(symbol="GFGC8000OC", action="buy", reason="test", iv_dislocation_vol_points=-5.0)
        bot._act_on_signal(signal, spot=7000.0)

        matching = [p for p in bot.portfolio.positions if p.symbol == "GFGC8000OC"]
        assert matching == [], "no debia abrirse ninguna posicion mientras la base este en cooldown"
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = original_cooldown


def test_act_on_signal_allowed_once_cooldown_expires():
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_cooldown = SETTINGS.vol_arbitrage.reentry_cooldown_seconds
    try:
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = 60.0
        bot = _make_vol_arbitrage_bot()
        _upsert_quote(bot, "GFGC8000OC", mid=180.0)
        # Cooldown ya vencido (timestamp en el pasado).
        bot._vol_arbitrage_reentry_cooldown_until["GFGC8000OC"] = time.time() - 1.0

        signal = TradeSignal(symbol="GFGC8000OC", action="buy", reason="test", iv_dislocation_vol_points=-5.0)
        bot._act_on_signal(signal, spot=7000.0)

        matching = [p for p in bot.portfolio.positions if p.symbol == "GFGC8000OC"]
        assert len(matching) == 1, "el cooldown ya vencido no debia bloquear la reentrada"
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = original_cooldown


def test_act_on_signal_ignores_cooldown_when_disabled_by_default():
    """reentry_cooldown_seconds=None (default) -> Guarda 0 es un no-op completo, aunque el dict tenga una entrada vieja."""
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    original_cooldown = SETTINGS.vol_arbitrage.reentry_cooldown_seconds
    try:
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = None
        bot = _make_vol_arbitrage_bot()
        _upsert_quote(bot, "GFGC8000OC", mid=180.0)
        bot._vol_arbitrage_reentry_cooldown_until["GFGC8000OC"] = time.time() + 6000.0

        signal = TradeSignal(symbol="GFGC8000OC", action="buy", reason="test", iv_dislocation_vol_points=-5.0)
        bot._act_on_signal(signal, spot=7000.0)

        matching = [p for p in bot.portfolio.positions if p.symbol == "GFGC8000OC"]
        assert len(matching) == 1
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow
        SETTINGS.vol_arbitrage.reentry_cooldown_seconds = original_cooldown


def test_check_vol_arbitrage_exits_ignores_other_strategy_tags():
    """Aislamiento: una posicion de weekly_asymmetric (o sin tag) en la misma perdida NO debe tocarse aca."""
    original_strategy = SETTINGS.strategy.active
    original_shadow = SETTINGS.shadow.enabled
    try:
        bot = _make_vol_arbitrage_bot()
        bot.portfolio.add(Position(
            symbol="GFGC8000OC", quantity=10, multiplier=100.0,
            greeks_per_unit={"delta": 0.3, "gamma": 0.001, "vega": 2.0, "theta": -1.0},
            expiry=date(2026, 12, 18),
            entry_price=180.0, entry_time=datetime.now(timezone.utc) - timedelta(days=13),
            strategy_tag="weekly_asymmetric",
        ))
        _upsert_quote(bot, "GFGC8000OC", mid=55.24)

        bot._check_vol_arbitrage_exits(spot=7000.0)

        remaining = sum(p.quantity for p in bot.portfolio.positions if p.symbol == "GFGC8000OC")
        assert remaining == 10, "una posicion marcada weekly_asymmetric no debe ser evaluada por este chequeo"
    finally:
        SETTINGS.strategy.active = original_strategy
        SETTINGS.shadow.enabled = original_shadow


ALL_TESTS = [
    test_act_on_signal_tags_new_positions_as_vol_arbitrage,
    test_check_vol_arbitrage_exits_closes_position_on_stop_loss,
    test_check_vol_arbitrage_exits_leaves_healthy_position_untouched,
    test_check_vol_arbitrage_exits_noop_when_disabled,
    test_check_vol_arbitrage_exits_stop_loss_sets_reentry_cooldown,
    test_check_vol_arbitrage_exits_no_cooldown_recorded_by_default,
    test_check_vol_arbitrage_exits_take_profit_does_not_set_cooldown,
    test_act_on_signal_blocked_during_reentry_cooldown,
    test_act_on_signal_allowed_once_cooldown_expires,
    test_act_on_signal_ignores_cooldown_when_disabled_by_default,
    test_check_vol_arbitrage_exits_ignores_other_strategy_tags,
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
