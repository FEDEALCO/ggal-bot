"""
test_fresh_greeks_for_vega_decay_exit.py
===========================================
RiskConfig.require_fresh_book_for_current_greeks (hallazgo de auditoria,
2026-10-09, a pedido explicito del usuario) - ver run_bot.py::
_run_weekly_asymmetric_cycle, donde se arma `current_greeks`.

OptionChain.recompute_all() (ver data/option_chain.py) solo recalcula
iv/greeks de una opcion cuando su book tiene bid>0 y ask>0 ese ciclo - si
la punta se vacio, deliberadamente NO recalcula, pero tampoco limpia el
iv/greeks ya calculado en un ciclo anterior (el objeto OptionQuote los
conserva). Hasta esta mejora, `current_greeks` en run_bot.py se armaba
tomando CUALQUIER quote con greeks != None, sin chequear si esa punta esta
viva ESTE ciclo - una posicion abierta cuya opcion se quedo sin cotizacion
podia comparar su vega de entrada contra una vega "actual" que en realidad
era stale (de la ultima vez que el book tuvo dos puntas), sin ninguna marca
de que lo era. Confirmado con datos reales de produccion
(market_snapshots.csv, ciclo 2026-10-09T01:56:04 UTC: filas con
bid=0.0/ask=0.0 pero iv/delta/gamma/vega/theta poblados).

Reproduce el escenario minimo: una posicion con vega de entrada alta
(vega_per_unit=10.0) y una compresion de vega que, si se mide contra el
numero STALE que quedo en el OptionQuote (vega=1.0, ratio=0.10, muy por
debajo del umbral default de 0.20), dispararia la salida por compresion de
vega aunque el book de esa opcion este vacio (bid=ask=0) este ciclo.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ggal_bot.validation import _shadow_audit_isolation  # noqa: F401

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OrderBookSnapshot, OptionQuote
from ggal_bot.models.black_scholes import OptionType
from ggal_bot.portfolio.portfolio import Position
from run_bot import GgalOptionsBot


def _setup_stale_vega_scenario() -> GgalOptionsBot:
    bot = GgalOptionsBot()
    now = datetime.now(timezone.utc)

    # Book VACIO este ciclo (sin cotizacion de ningun lado) pero con
    # iv/greeks que quedaron de la ULTIMA vez que tuvo una punta valida -
    # exactamente el patron real observado en market_snapshots.csv.
    empty_book = OrderBookSnapshot("GFGC7000OC", bid=0.0, ask=0.0, bid_size=0.0, ask_size=0.0)
    stale_quote = OptionQuote(
        symbol="GFGC7000OC", strike=7000.0, expiry=date(2026, 10, 16),
        option_type=OptionType.CALL, book=empty_book, days_calendar=8, days_business=6,
    )
    stale_quote.iv = 0.40
    stale_quote.greeks = {"delta": 0.50, "gamma": 0.0008, "vega": 1.0, "theta": -1.0, "rho": 0.1}
    bot.option_chain.upsert_quote(stale_quote)

    # Posicion abierta hace 10h (supera vega_decay_min_holding_hours=3.0
    # default) con vega de entrada ALTA (congelada al fill, nunca se
    # actualiza) - vega_actual_stale(1.0)/vega_entrada(10.0) = 0.10, por
    # debajo del umbral default de 0.20: SI dispararia la salida por
    # compresion de vega si `current_vega` tomara el numero stale.
    bot.portfolio.add(Position(
        symbol="GFGC7000OC", quantity=1.0, multiplier=100.0,
        greeks_per_unit={"delta": 0.55, "gamma": 0.0009, "vega": 10.0, "theta": -1.4},
        entry_price=25.5, entry_time=now - timedelta(hours=10),
        expiry=date(2026, 10, 16), strategy_tag="weekly_asymmetric",
    ))
    return bot


def _vega_decay_signals(bot: GgalOptionsBot):
    signals = bot._run_weekly_asymmetric_cycle(spot=7000.0)
    return [
        s for s in signals
        if getattr(s, "symbol", None) == "GFGC7000OC" and "vega" in str(getattr(s, "reason", "")).lower()
    ]


class _IsolatedVegaDecaySettings:
    """
    Guarda/restaura los settings que tocan estos tests, y ademas APAGA
    weekend_theta_guard (LongFirstConfig.weekend_theta_guard_enabled,
    default True) - sin esto, con `entry_time` 10hs atras, ese guard puede
    disparar primero y tapar la salida por compresion de vega bajo
    evaluacion (reason is None es requisito para que vega-decay se evalue,
    ver weekly_asymmetric.py:1019), que es lo unico que estos tests quieren
    aislar.
    """

    def __init__(self, require_fresh_book: bool):
        self.require_fresh_book = require_fresh_book

    def __enter__(self):
        self._shadow = SETTINGS.shadow.enabled
        self._flag = SETTINGS.risk.require_fresh_book_for_current_greeks
        self._weekend_guard = SETTINGS.long_first.weekend_theta_guard_enabled
        SETTINGS.shadow.enabled = True
        SETTINGS.risk.require_fresh_book_for_current_greeks = self.require_fresh_book
        SETTINGS.long_first.weekend_theta_guard_enabled = False
        return self

    def __exit__(self, *exc_info):
        SETTINGS.shadow.enabled = self._shadow
        SETTINGS.risk.require_fresh_book_for_current_greeks = self._flag
        SETTINGS.long_first.weekend_theta_guard_enabled = self._weekend_guard


def test_stale_greeks_do_not_trigger_vega_decay_exit_when_book_is_empty():
    """
    Con el fix activo (default), una opcion sin book vivo este ciclo no
    debe poder disparar la salida por compresion de vega usando su ultimo
    iv/greeks conocido - `current_greeks` debe excluirla, current_vega
    llega None a evaluate_vega_decay_exit, y esa salida simplemente no se
    evalua este ciclo (misma semantica que "greeks desconocidas" en el
    resto del proyecto).
    """
    with _IsolatedVegaDecaySettings(require_fresh_book=True):
        bot = _setup_stale_vega_scenario()
        vega_signals = _vega_decay_signals(bot)
        assert not vega_signals, (
            f"No deberia dispararse una salida por compresion de vega usando greeks stale "
            f"de un book vacio - señales encontradas: {vega_signals}"
        )


def test_fresh_greeks_still_trigger_vega_decay_exit_when_book_is_live():
    """
    Control: el MISMO escenario de compresion de vega, pero con el book
    vivo (bid/ask>0) este ciclo - la salida SI debe evaluarse y disparar,
    confirmando que el fix no rompe el caso real que la salida esta
    pensada para cubrir.
    """
    with _IsolatedVegaDecaySettings(require_fresh_book=True):
        bot = _setup_stale_vega_scenario()
        live_book = OrderBookSnapshot("GFGC7000OC", bid=24.0, ask=26.0, bid_size=50.0, ask_size=50.0)
        live_quote = bot.option_chain.get("GFGC7000OC")
        live_quote.book = live_book
        vega_signals = _vega_decay_signals(bot)
        assert vega_signals, "Con el book vivo, la compresion de vega real debe seguir disparando la salida."


def test_stale_greeks_trigger_vega_decay_exit_when_flag_disabled():
    """
    Con RiskConfig.require_fresh_book_for_current_greeks=False
    (GGAL_BOT_REQUIRE_FRESH_BOOK_FOR_CURRENT_GREEKS=false), se reproduce el
    comportamiento previo a esta mejora - disponible para desactivar
    explicitamente (ej. debugging local/tests que inyectan greeks sin
    pasar por un book real).
    """
    with _IsolatedVegaDecaySettings(require_fresh_book=False):
        bot = _setup_stale_vega_scenario()
        vega_signals = _vega_decay_signals(bot)
        assert vega_signals, (
            "Con el flag desactivado explicitamente, deberia reproducirse el comportamiento "
            "previo (usar el iv/greeks stale igual, sin chequear el book)."
        )


ALL_TESTS = [
    test_stale_greeks_do_not_trigger_vega_decay_exit_when_book_is_empty,
    test_fresh_greeks_still_trigger_vega_decay_exit_when_book_is_live,
    test_stale_greeks_trigger_vega_decay_exit_when_flag_disabled,
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
