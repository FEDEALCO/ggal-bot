"""
test_fase53_kill_switch.py
============================
Tests de regresion (Fase 5.3, COMMIT 3) para ggal_bot/risk/kill_switch.py:
persistencia en disco (sobrevive a "restart" = una nueva instancia de
KillSwitch apuntando al mismo archivo), evaluacion de limites agregados de
portfolio, y la garantia de diseño de que el kill switch bloquea SOLO
entradas nuevas, nunca salidas.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS, RiskLimitsConfig
from ggal_bot.data.option_chain import OrderBookSnapshot, OptionQuote
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Portfolio, Position
from ggal_bot.risk.kill_switch import KillSwitch
from ggal_bot.strategy.weekly_asymmetric import EntrySignal
from run_bot import GgalOptionsBot
from datetime import date


def _make_quote(symbol: str) -> OptionQuote:
    book = OrderBookSnapshot(symbol, bid=570.0, ask=575.0, bid_size=50, ask_size=50)
    quote = OptionQuote(
        symbol=symbol, strike=7000.0, expiry=date(2026, 9, 18),
        option_type=OptionType.CALL, book=book, days_calendar=17, days_business=12,
    )
    quote.greeks = {"delta": 0.55, "gamma": 0.0009, "vega": 3.2, "theta": -1.4, "rho": 0.2, "price": 572.5}
    quote.iv = 0.55
    return quote


def test_trip_and_status_persist_across_new_instances(tmp_path):
    path = tmp_path / "kill_switch.json"
    ks1 = KillSwitch(path=path)
    assert ks1.is_tripped() is False

    ks1.trip("prueba de persistencia", tripped_by="test")

    # Simula un restart de proceso: instancia NUEVA, mismo archivo.
    ks2 = KillSwitch(path=path)
    assert ks2.is_tripped() is True
    state = ks2.status()
    assert state.reason == "prueba de persistencia"
    assert state.tripped_by == "test"


def test_reset_clears_trip(tmp_path):
    path = tmp_path / "kill_switch.json"
    ks = KillSwitch(path=path)
    ks.trip("motivo")
    assert ks.is_tripped() is True
    ks.reset("ya revisado")
    assert ks.is_tripped() is False


def test_missing_file_reports_not_tripped(tmp_path):
    ks = KillSwitch(path=tmp_path / "no_existe.json")
    assert ks.is_tripped() is False


def test_evaluate_trips_on_max_open_contracts(tmp_path):
    ks = KillSwitch(path=tmp_path / "ks.json")
    limits = RiskLimitsConfig(max_open_contracts_total=5.0)

    class _FakePortfolio:
        positions = [
            Position(symbol="A", quantity=3.0, multiplier=100.0),
            Position(symbol="B", quantity=4.0, multiplier=100.0),
        ]

    reason = ks.evaluate(_FakePortfolio(), limits)
    assert reason is not None
    assert ks.is_tripped() is True


def test_evaluate_does_not_trip_under_limits(tmp_path):
    ks = KillSwitch(path=tmp_path / "ks.json")
    limits = RiskLimitsConfig(max_open_contracts_total=100.0)

    class _FakePortfolio:
        positions = [Position(symbol="A", quantity=3.0, multiplier=100.0)]

    reason = ks.evaluate(_FakePortfolio(), limits)
    assert reason is None
    assert ks.is_tripped() is False


def test_evaluate_trips_on_fragmentation_integrity_violation(tmp_path):
    ks = KillSwitch(path=tmp_path / "ks.json")
    limits = RiskLimitsConfig(max_positions_per_symbol_strategy=1)

    class _FakePortfolio:
        positions = [
            Position(symbol="GFGC7000OC", quantity=3.0, multiplier=100.0, strategy_tag="weekly_asymmetric"),
            Position(symbol="GFGC7000OC", quantity=3.0, multiplier=100.0, strategy_tag="weekly_asymmetric"),
        ]

    reason = ks.evaluate(_FakePortfolio(), limits)
    assert reason is not None and "Integridad" in reason
    assert ks.is_tripped() is True


def test_evaluate_disabled_never_trips(tmp_path):
    ks = KillSwitch(path=tmp_path / "ks.json")
    limits = RiskLimitsConfig(enabled=False, max_open_contracts_total=1.0)

    class _FakePortfolio:
        positions = [Position(symbol="A", quantity=999.0, multiplier=100.0)]

    reason = ks.evaluate(_FakePortfolio(), limits)
    assert reason is None
    assert ks.is_tripped() is False


def test_evaluate_trips_on_correlated_delta_across_different_symbols(tmp_path):
    """
    RiskLimitsConfig.max_portfolio_delta_ars (mega-prompt "OPTIMIZACION
    EJECUTABLE", seccion 12): tres posiciones en simbolos/strikes
    DISTINTOS, cada una individualmente chica, que en la MISMA direccion
    (todas calls largos) acumulan un delta de portfolio grande - exactamente
    el patron "Trade A ok + Trade B ok + Trade C ok = riesgo excesivo" que
    ni max_open_contracts_total ni max_positions_per_symbol_strategy (ambos
    por-base, no agregados-direccionales) pueden detectar.
    """
    ks = KillSwitch(path=tmp_path / "ks.json")
    limits = RiskLimitsConfig(max_portfolio_delta_ars=1_000_000.0)

    portfolio = Portfolio()
    for symbol, strike in (("GFGC7000OC", 7000.0), ("GFGC7200OC", 7200.0), ("GFGC7400OC", 7400.0)):
        portfolio.add(Position(
            symbol=symbol, quantity=10.0, multiplier=100.0,
            greeks_per_unit={"delta": 0.5, "gamma": 0.0009, "vega": 3.2, "theta": -1.4},
            strategy_tag="weekly_asymmetric",
        ))
    # delta total = 3 * (10 * 100 * 0.5) = 1500 acciones equiv.; spot=7100 ->
    # notional = 1500 * 7100 = 10.650.000 ARS, muy por encima del limite.
    reason = ks.evaluate(portfolio, limits, spot=7100.0)
    assert reason is not None and "direccional agregada" in reason
    assert ks.is_tripped() is True


def test_evaluate_skips_delta_check_when_spot_not_provided(tmp_path):
    """
    Simetrico a como max_daily_loss_ars se omite sin realized_pnl_today_ars:
    si max_portfolio_delta_ars esta configurado pero el caller no pasa
    `spot`, el chequeo NO se evalua (nunca se fabrica un spot ficticio) -
    no debe disparar el kill switch aunque el delta acumulado sea enorme.
    """
    ks = KillSwitch(path=tmp_path / "ks.json")
    limits = RiskLimitsConfig(max_portfolio_delta_ars=1.0)  # limite absurdamente bajo

    portfolio = Portfolio()
    portfolio.add(Position(
        symbol="GFGC7000OC", quantity=100.0, multiplier=100.0,
        greeks_per_unit={"delta": 0.9, "gamma": 0.0009, "vega": 3.2, "theta": -1.4},
        strategy_tag="weekly_asymmetric",
    ))

    reason = ks.evaluate(portfolio, limits)  # sin spot=
    assert reason is None
    assert ks.is_tripped() is False


def test_tripped_kill_switch_blocks_new_entry_but_never_blocks_exit(tmp_path):
    """
    Integracion contra el bot real: con el kill switch disparado,
    _act_on_entry_signal no debe abrir ninguna posicion nueva;
    _act_on_exit_signal (sobre una posicion YA abierta, creada antes de que
    el switch se disparara) debe poder cerrarla igual - la garantia central
    de diseño (ver docstring de KillSwitch: "por que NUNCA bloquea salidas").

    Usa un `KillSwitch` con path DEDICADO (tmp_path), no el default de
    `bot.kill_switch` (que apunta al archivo compartido de aislamiento de
    tests, ver _shadow_audit_isolation.py) - un trip() persistido ahi
    sobreviviria a este test y "contaminaria" cualquier otro test que
    despues construya un GgalOptionsBot() nuevo en la MISMA corrida de
    pytest (un archivo persistido, a diferencia de un atributo en memoria,
    no se resetea solo entre tests). Mismo criterio que
    test_order_gateway_shadow_mode_logs_fill_to_audit_csv usa para
    ShadowAuditLogger.
    """
    original_enabled = SETTINGS.shadow.enabled
    SETTINGS.shadow.enabled = True
    try:
        bot = GgalOptionsBot()
        bot.kill_switch = KillSwitch(path=tmp_path / "kill_switch_isolated.json")
        bot.option_chain.upsert_quote(_make_quote("GFGC7000OC"))
        bot.option_chain.upsert_quote(_make_quote("GFGC7200OC"))

        # Posicion preexistente sobre OTRA base, para poder probar el cierre.
        bot.portfolio.add(Position(
            symbol="GFGC7200OC", quantity=3.0, multiplier=100.0,
            entry_price=572.5, entry_time=datetime.now(timezone.utc),
            strategy_tag="weekly_asymmetric",
        ))

        bot.kill_switch.trip("prueba de bloqueo de entradas")

        # Entrada nueva sobre una base DISTINTA: debe quedar bloqueada.
        entry_signal = EntrySignal(
            symbol="GFGC7000OC", option_type=OptionType.CALL, reason="test_kill_switch",
            premium_reference=572.5, iv_dislocation_vol_points=5.0, convexity_score=0.01,
        )
        bot._act_on_entry_signal(entry_signal, spot=7050.0)
        assert bot._position_quantity("GFGC7000OC") == 0.0, "el kill switch debia bloquear la entrada nueva"

        # Salida sobre la posicion YA abierta: debe funcionar igual.
        class _ExitSig:
            symbol = "GFGC7200OC"
            reason = "stop_loss"
            action = "sell_to_close"
            quantity = 3.0

        bot._act_on_exit_signal(_ExitSig(), spot=7040.0)
        assert bot._position_quantity("GFGC7200OC") == 0.0, (
            "BUG: el kill switch bloqueo una salida - nunca debe hacerlo (ver docstring de KillSwitch)."
        )
    finally:
        SETTINGS.shadow.enabled = original_enabled
