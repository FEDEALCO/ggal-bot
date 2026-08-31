"""
test_quant_engine.py
=====================
Tests de sanity del motor cuantitativo, sin dependencia de mercado real ni
de PyRofex. Corren tanto con pytest (si esta instalado) como directamente
con `python -m ggal_bot.validation.test_quant_engine`.

Cubre lo minimo indispensable antes de pasar a paper trading (ver checklist
en docs/Diseno_Bot_Opciones_GGAL.md):
    1. Paridad put-call sobre el pricer.
    2. Convergencia del solver de IV (recupera la sigma verdadera).
    3. Ajuste de smile detecta dislocaciones sinteticas conocidas.
    4. Limites de riesgo y delta-hedger disparan cuando corresponde.
"""

from __future__ import annotations

import os
import sys

# Permite correr este archivo tanto como modulo (`python -m
# ggal_bot.validation.test_quant_engine`, recomendado) como script directo
# (`python test_quant_engine.py` parado en esta carpeta, o doble-click/Run
# desde un editor): en ese segundo caso Python no agrega la raiz del
# proyecto a sys.path por si solo, y el `import ggal_bot...` de abajo
# fallaria con ModuleNotFoundError.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import math
from datetime import date

from ggal_bot.models.black_scholes import BlackScholesGreeks, OptionType
from ggal_bot.models.implied_vol import ImpliedVolatilityCalculator
from ggal_bot.models.historical_volatility import HistoricalVolatility
from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.data.option_chain import OrderBookSnapshot, OptionQuote, OptionChain
from ggal_bot.portfolio.portfolio import Position, Portfolio
from ggal_bot.risk.risk_manager import RiskLimits, RiskManager
from ggal_bot.strategy.delta_hedger import DeltaHedgingEngine


def test_put_call_parity():
    spot, strike, rate = 5200.0, 5200.0, 0.40
    days_cal, days_biz, sigma = 30, 21, 0.55

    call = BlackScholesGreeks(spot, strike, rate, days_calendar=days_cal,
                               days_business=days_biz, option_type=OptionType.CALL)
    put = BlackScholesGreeks(spot, strike, rate, days_calendar=days_cal,
                              days_business=days_biz, option_type=OptionType.PUT)

    call_price = call.price(sigma)
    put_price = put.price(sigma)
    disc_r = math.exp(-rate * call.t_rate)
    lhs = call_price - put_price
    rhs = spot - strike * disc_r
    assert abs(lhs - rhs) < 1e-6, f"Paridad put-call rota: LHS={lhs} RHS={rhs}"


def test_iv_solver_recovers_true_sigma():
    spot, strike, rate = 5200.0, 5200.0, 0.40
    days_cal, days_biz, true_sigma = 30, 21, 0.55

    call = BlackScholesGreeks(spot, strike, rate, days_calendar=days_cal,
                               days_business=days_biz, option_type=OptionType.CALL)
    price = call.price(true_sigma)

    iv_calc = ImpliedVolatilityCalculator()
    recovered = iv_calc.solve(call, price, sigma_guess=0.30)
    assert recovered is not None
    assert abs(recovered - true_sigma) < 1e-4, f"IV recuperada {recovered} != {true_sigma}"


def test_iv_solver_deep_otm_falls_back_to_bisection():
    """Vega casi nula en strikes muy alejados: Newton-Raphson debe fallar y caer a biseccion."""
    spot, strike, rate = 5200.0, 9000.0, 0.40
    days_cal, days_biz, true_sigma = 10, 7, 0.55

    call = BlackScholesGreeks(spot, strike, rate, days_calendar=days_cal,
                               days_business=days_biz, option_type=OptionType.CALL)
    price = call.price(true_sigma)
    if price < 1e-6:
        return  # precio no operable, no hay nada que recuperar

    iv_calc = ImpliedVolatilityCalculator()
    recovered = iv_calc.solve(call, price, sigma_guess=0.30)
    assert recovered is not None
    assert abs(recovered - true_sigma) < 1e-2


def _build_quotes(strikes, ivs, spot=5200.0, rate=0.40, days_cal=30, days_biz=21):
    iv_calc = ImpliedVolatilityCalculator()
    quotes = []
    for k, iv in zip(strikes, ivs):
        bs = BlackScholesGreeks(spot, k, rate, days_calendar=days_cal,
                                 days_business=days_biz, option_type=OptionType.CALL)
        price = bs.price(iv)
        spread = price * 0.02
        book = OrderBookSnapshot(f"GFGC{k}", bid=price - spread / 2, ask=price + spread / 2,
                                  bid_size=100, ask_size=100)
        q = OptionQuote(f"GFGC{k}", strike=k, expiry=date(2026, 10, 16),
                         option_type=OptionType.CALL, book=book,
                         days_calendar=days_cal, days_business=days_biz)
        q.compute_iv_and_greeks(spot, rate, iv_calc)
        quotes.append(q)
    return quotes


def test_smile_dislocation_null_case_with_smooth_symmetric_smile():
    """Con un smile simetrico y suave (sin ninguna base realmente mispriced),
    ninguna base deberia mostrar una dislocacion grande contra su propia
    curva ajustada. Este es el caso NULO (sin falso positivo) - ver
    test_smile_dislocation_detects_real_bump_with_enough_points_for_quadratic
    para el caso de verdadero positivo (deteccion real de un salto)."""
    strikes = [4800, 5000, 5100, 5200, 5300, 5400, 5600]
    true_ivs = [0.62, 0.58, 0.56, 0.55, 0.56, 0.58, 0.63]  # smile simetrico
    quotes = _build_quotes(strikes, true_ivs)

    surface = VolatilitySurface(quotes)
    assert surface.fit_degree == 2  # 7 puntos: alcanza el minimo para la cuadratica
    dislocations = {q.symbol: surface.smile_dislocation(q) for q in quotes}
    assert max(abs(v) for v in dislocations.values()) < 2.0


def test_smile_dislocation_detects_real_bump_with_enough_points_for_quadratic():
    """
    Regresion del bug real de VolatilitySurface (ver
    docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.2): a diferencia del test
    anterior (caso nulo), este SI inyecta una dislocacion real - un salto de
    15 vol points en el strike del medio de un smile por lo demas suave - y
    verifica que efectivamente se detecta (verdadero positivo). El test
    viejo con este nombre nunca hacia esto (solo probaba el caso nulo); el
    bug de umbral minimo (3 puntos = interpolacion perfecta de la cuadratica,
    residuo cero SIEMPRE) hacia que este caso jamas se hubiera detectado si
    alguien lo hubiera escrito con solo 3 puntos.
    """
    strikes = [4800, 5000, 5100, 5200, 5300, 5400, 5600]
    smooth_ivs = [0.62, 0.58, 0.56, 0.55, 0.56, 0.58, 0.63]
    bumped_ivs = list(smooth_ivs)
    bumped_ivs[3] = smooth_ivs[3] + 0.15  # +15 vol points en el strike 5200 (ATM)
    quotes = _build_quotes(strikes, bumped_ivs)

    surface = VolatilitySurface(quotes)
    assert surface.fit_degree == 2
    bumped_quote = next(q for q in quotes if q.strike == 5200)
    other_quotes = [q for q in quotes if q.strike != 5200]

    bumped_dislocation = surface.smile_dislocation(bumped_quote)
    other_dislocations = [surface.smile_dislocation(q) for q in other_quotes]

    assert bumped_dislocation > 8.0, f"dislocacion del salto inyectado deberia ser grande, fue {bumped_dislocation:.2f}"
    # Los vecinos inmediatos absorben algo de "arrastre" del ajuste cuadratico
    # (la curva se curva hacia el salto), asi que no estaran exactamente en
    # cero - pero deben quedar claramente por debajo de la base perturbada.
    assert max(abs(v) for v in other_dislocations) < 5.0, "las bases no perturbadas no deberian mostrar dislocacion grande"
    assert bumped_dislocation > 2 * max(abs(v) for v in other_dislocations), (
        "la base perturbada deberia dominar claramente sobre el arrastre en sus vecinos"
    )


def test_smile_dislocation_degenerate_three_point_case_falls_back_to_linear():
    """
    Con exactamente 3 strikes validos (el minimo absoluto que acepta
    VolatilitySurface), ya NO se usa el ajuste cuadratico de 3 parametros
    (que interpolaria perfectamente y reportaria dislocacion ~0 sin importar
    el salto) - se usa un ajuste lineal de 2 parametros, que con 3 puntos
    deja 1 grado de libertad de residuo real. No es tan sensible como con
    mas puntos, pero al menos no queda matematicamente ciego al mispricing.
    """
    strikes = [5000, 5200, 5400]
    ivs = [0.55, 0.70, 0.55]  # salto de 15 vol points en el strike del medio
    quotes = _build_quotes(strikes, ivs)

    surface = VolatilitySurface(quotes)
    assert surface.fit_degree == 1  # 3 puntos: por debajo del minimo para la cuadratica
    bumped_quote = next(q for q in quotes if q.strike == 5200)
    dislocation = surface.smile_dislocation(bumped_quote)
    # Ya no es exactamente 0 (el bug viejo): el ajuste lineal no puede
    # capturar el salto simetrico perfectamente, pero deja residuo real.
    assert abs(dislocation) > 1.0, f"con el bug viejo esto daba ~0; ahora deberia quedar un residuo real, fue {dislocation:.2f}"


def test_theta_matches_finite_difference_across_both_day_count_clocks():
    """
    Regresion del bug real de theta (ver docs/AUDITORIA_MAESTRA_2026-08-27.md
    seccion 3.1 / black_scholes.py:theta()): la formula vieja dividia TODA la
    theta anualizada por 252, mezclando el reloj de dias habiles (t_vol, que
    gobierna el termino de decaimiento por volatilidad) con el de dias
    corridos (t_rate, que gobierna el termino de carry de tasa/dividendo).//
    Este test verifica la theta analitica contra una diferencia finita real:
    hacer pasar un dia corrido Y un dia habil a la vez (que es lo que
    efectivamente ocurre en una rueda normal) y comparar
    -(price(t-1) - price(t))/1dia contra theta(). Con dividend_yield>0 para
    ejercitar tambien el termino de carry por dividendo, no solo el de tasa.
    """
    spot, strike, rate, dividend_yield = 5200.0, 5200.0, 0.40, 0.02
    days_cal, days_biz, sigma = 30, 21, 0.55

    call = BlackScholesGreeks(spot, strike, rate, dividend_yield=dividend_yield,
                               days_calendar=days_cal, days_business=days_biz,
                               option_type=OptionType.CALL)
    call_minus_1d = BlackScholesGreeks(spot, strike, rate, dividend_yield=dividend_yield,
                                        days_calendar=days_cal - 1, days_business=days_biz - 1,
                                        option_type=OptionType.CALL)
    finite_diff_theta = call_minus_1d.price(sigma) - call.price(sigma)
    analytic_theta = call.theta(sigma)
    # Tolerancia laxa (no es una derivada exacta, es un paso finito de 1 dia
    # calendario + 1 dia habil simultaneos) pero suficiente para atrapar un
    # error sistematico de ~12% de magnitud como el que tenia el bug real.
    assert abs(analytic_theta - finite_diff_theta) < 0.15, (
        f"theta analitica {analytic_theta:.4f} vs diferencia finita {finite_diff_theta:.4f}"
    )

    put = BlackScholesGreeks(spot, strike, rate, dividend_yield=dividend_yield,
                              days_calendar=days_cal, days_business=days_biz,
                              option_type=OptionType.PUT)
    put_minus_1d = BlackScholesGreeks(spot, strike, rate, dividend_yield=dividend_yield,
                                       days_calendar=days_cal - 1, days_business=days_biz - 1,
                                       option_type=OptionType.PUT)
    finite_diff_theta_put = put_minus_1d.price(sigma) - put.price(sigma)
    analytic_theta_put = put.theta(sigma)
    assert abs(analytic_theta_put - finite_diff_theta_put) < 0.15, (
        f"theta put analitica {analytic_theta_put:.4f} vs diferencia finita {finite_diff_theta_put:.4f}"
    )


def test_order_book_snapshot_staleness_helpers():
    """
    Regresion del hallazgo del 2026-08-31 (ver docs/AUDITORIA_MAESTRA_2026-08-27.md,
    seguimiento): OrderBookSnapshot no tenia forma de saber cuando fue
    observada realmente una punta - una fuente que reproduce la ultima
    cotizacion cacheada (ver BrokerRestSource.fetch_snapshot()) la despachaba
    como si fuera fresca en cada poll. `as_of`/`age_seconds`/`is_stale` cierran
    ese hueco.
    """
    now = 1_000_000.0
    fresh = OrderBookSnapshot("GGAL", bid=100.0, ask=101.0, bid_size=10, ask_size=10, as_of=now - 5.0)
    stale = OrderBookSnapshot("GGAL", bid=100.0, ask=101.0, bid_size=10, ask_size=10, as_of=now - 500.0)

    assert fresh.age_seconds(now=now) == 5.0
    assert stale.age_seconds(now=now) == 500.0
    assert fresh.is_stale(90.0, now=now) is False
    assert stale.is_stale(90.0, now=now) is True

    # Default (sin as_of explicito): recien creado, nunca deberia leerse como stale.
    just_created = OrderBookSnapshot("GGAL", bid=100.0, ask=101.0, bid_size=10, ask_size=10)
    assert just_created.is_stale(1.0) is False


def test_recompute_all_excludes_stale_quotes_from_iv_refresh():
    """
    Regresion del hallazgo del 2026-08-31 (ver RiskConfig.
    max_option_quote_staleness_seconds): antes de esto, recompute_all()
    recalculaba IV/griegas de TODAS las opciones contra el spot actual, sin
    importar que tan vieja fuera la punta de esa opcion puntual - mezclando
    un spot fresco con un precio de opcion viejo (cadena de opciones caida
    sola, ver live_shadow_feed.BrokerRestSource, mientras el spot seguia
    bien). Este test verifica que, con max_quote_age_seconds seteado, una
    opcion stale NO se recalcula (se congela en su ultimo IV conocido)
    mientras una opcion fresca si se recalcula con normalidad, y que el
    conteo de opciones salteadas por staleness es el esperado.
    """
    spot, rate, days_cal, days_biz, sigma_guess = 5200.0, 0.40, 30, 21, 0.35
    iv_calc = ImpliedVolatilityCalculator()
    now = 1_000_000.0

    def make_quote(symbol: str, strike: float, true_sigma: float, as_of: float) -> OptionQuote:
        bs = BlackScholesGreeks(spot, strike, rate, days_calendar=days_cal,
                                 days_business=days_biz, option_type=OptionType.CALL)
        price = bs.price(true_sigma)
        spread = price * 0.02
        book = OrderBookSnapshot(symbol, bid=price - spread / 2, ask=price + spread / 2,
                                  bid_size=100, ask_size=100, as_of=as_of)
        quote = OptionQuote(symbol, strike=strike, expiry=date(2026, 10, 16),
                             option_type=OptionType.CALL, book=book,
                             days_calendar=days_cal, days_business=days_biz)
        quote.compute_iv_and_greeks(spot, rate, iv_calc, sigma_guess=sigma_guess)
        return quote

    fresh_quote = make_quote("GFGC5200", 5200.0, 0.55, as_of=now - 5.0)
    stale_quote = make_quote("GFGC5300", 5300.0, 0.55, as_of=now - 500.0)
    iv_before_fresh = fresh_quote.iv
    iv_before_stale = stale_quote.iv
    assert iv_before_fresh is not None and iv_before_stale is not None

    chain = OptionChain()
    chain.upsert_quote(fresh_quote)
    chain.upsert_quote(stale_quote)

    new_spot = spot * 1.01  # spot se mueve un poco: cambia el IV de la opcion fresca sin romper la convergencia del solver
    stale_count = chain.recompute_all(
        spot=new_spot, rate=rate, iv_calc=iv_calc, sigma_guess=sigma_guess,
        max_quote_age_seconds=90.0, now=now,
    )

    assert stale_count == 1
    assert fresh_quote.iv is not None and fresh_quote.iv != iv_before_fresh, (
        "la opcion fresca deberia haberse recalculado contra el nuevo spot"
    )
    assert stale_quote.iv == iv_before_stale, (
        "la opcion stale NO deberia haberse recalculado (se congela en su ultimo IV conocido)"
    )

    # Sin max_quote_age_seconds (comportamiento por defecto, sin cambios): recalcula todo.
    stale_quote_2 = make_quote("GFGC5400", 5400.0, 0.55, as_of=now - 500.0)
    chain2 = OptionChain()
    chain2.upsert_quote(stale_quote_2)
    iv_before = stale_quote_2.iv
    result = chain2.recompute_all(spot=new_spot, rate=rate, iv_calc=iv_calc, sigma_guess=sigma_guess)
    assert result is None
    assert stale_quote_2.iv != iv_before


def test_risk_manager_halts_on_vega_breach():
    limits = RiskLimits(max_vega_total=1000.0, max_gamma_total=10_000.0)
    risk_mgr = RiskManager(limits)
    totals = {"delta": 0.0, "gamma": 1.0, "vega": 5000.0, "theta": 0.0}
    assert risk_mgr.should_halt_new_positions(totals) is True

    totals_ok = {"delta": 0.0, "gamma": 1.0, "vega": 500.0, "theta": 0.0}
    assert risk_mgr.should_halt_new_positions(totals_ok) is False


def test_delta_hedger_triggers_and_routes_to_contado():
    hedger = DeltaHedgingEngine(delta_band=150.0)
    assert hedger.needs_hedge(-500.0) is True
    assert hedger.needs_hedge(50.0) is False

    contado_book = OrderBookSnapshot("GGAL", bid=5199.0, ask=5201.0, bid_size=500, ask_size=500)
    instruction = hedger.build_hedge(-500.0, contado_book, futuro_book=None)
    assert instruction is not None
    assert instruction.instrument == "GGAL_CONTADO"
    # Se neutraliza el EXCEDENTE por sobre la banda (500 - 150 = 350), no el
    # delta total: llevarlo a cero implicaria rehedgear por cada variacion minima.
    assert instruction.quantity == 350.0


def test_historical_volatility_close_to_close_positive():
    closes = [5000, 5050, 5120, 5080, 5200, 5150, 5210, 5190, 5230, 5200]
    hv = HistoricalVolatility.close_to_close(closes)
    assert hv > 0


def test_portfolio_greeks_aggregation():
    spot, strike, rate, days_cal, days_biz, sigma = 5200.0, 5200.0, 0.40, 30, 21, 0.55
    call = BlackScholesGreeks(spot, strike, rate, days_calendar=days_cal,
                               days_business=days_biz, option_type=OptionType.CALL)
    greeks = call.all_greeks(sigma)

    portfolio = Portfolio()
    portfolio.add(Position("GGAL", quantity=100, multiplier=1, greeks_per_unit=None))
    portfolio.add(Position("GFGC5200O", quantity=-10, multiplier=100,
                            greeks_per_unit=greeks, expiry=date(2026, 10, 16)))
    totals = portfolio.total_greeks()
    expected_delta = 100 - 10 * 100 * greeks["delta"]
    assert abs(totals["delta"] - expected_delta) < 1e-6


ALL_TESTS = [
    test_put_call_parity,
    test_iv_solver_recovers_true_sigma,
    test_iv_solver_deep_otm_falls_back_to_bisection,
    test_smile_dislocation_null_case_with_smooth_symmetric_smile,
    test_smile_dislocation_detects_real_bump_with_enough_points_for_quadratic,
    test_smile_dislocation_degenerate_three_point_case_falls_back_to_linear,
    test_theta_matches_finite_difference_across_both_day_count_clocks,
    test_order_book_snapshot_staleness_helpers,
    test_recompute_all_excludes_stale_quotes_from_iv_refresh,
    test_risk_manager_halts_on_vega_breach,
    test_delta_hedger_triggers_and_routes_to_contado,
    test_historical_volatility_close_to_close_positive,
    test_portfolio_greeks_aggregation,
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
