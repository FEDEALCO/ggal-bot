"""
scalping.py
=============
Estrategia "Scalping Intradia y Trading Semanal de Corto Plazo" - modulo
ADITIVO, completamente independiente de WeeklyAsymmetricStrategy (ver la
nota de arquitectura junto a config.ScalpingConfig y
run_bot.py:_run_scalping_cycle). Mismo espiritu Long-First que el modo
original (unica direccion permitida: BUY to Open, nunca posiciones
descubiertas - ver LongFirstConfig.forbid_naked_short, el mismo principio
aplica aca aunque ScalpingConfig no repita el flag) pero con:

    - Horizonte de HOLDING en MINUTOS (no dias habiles) y cierre
      obligatorio de Fin de Dia (EOD) - nunca se sostiene una posicion de
      scalping de un dia para el otro (ver
      risk.risk_manager.RiskManager.evaluate_scalping_exit).
    - Tendencia INTRADIA multi-timeframe (5m/15m, ver
      data/intraday_bars.py) en vez del filtro diario 1D.
    - Filtro de profundidad minima de ASK ademas del OBI existente (ver
      models/microstructure.py.passes_min_ask_depth).
    - Salida adicional por REVERSION de la dislocacion de IV que motivo la
      entrada (ver data/iv_mean_reversion.py).
    - Sizing/capital PROPIO y separado (ver config.ScalpingConfig.
      max_capital_ars/max_risk_pct_per_trade/max_concurrent_positions),
      pensado para repartir el mismo capital total en MAS posiciones de
      MENOR tamaño que weekly_asymmetric.

Reutiliza (COMPOSICION, no herencia) WeeklyAsymmetricStrategy para el
escaneo de ENTRADAS: `scan_entry_signals()` de esa clase ya es generico -
recibe todos sus umbrales desde `self.cfg` y la tendencia/momentum como
strings INYECTADOS (nunca calculados internamente), sin ningun acoplamiento
a "grafico diario" en particular (ver docstring de weekly_asymmetric.py) -
alcanza con pasarle este mismo ScalpingConfig (que expone deliberadamente
los mismos nombres de atributo que ese metodo necesita) y la tendencia
INTRADIA en vez de la 1D. NO se reutiliza `build_exit_signals()` ni
`scan_spread_completion_signals()` de esa clase: los criterios de salida
son sustancialmente distintos (minutos vs dias habiles, EOD, reversion de
IV) y este modo, deliberadamente, no arma spreads (Bull Call/Bear Put) -
solo Long Call/Long Put desnudas de alta rotacion, para mantener el modulo
simple y sus riesgos acotados a "perder la prima pagada", igual que
weekly_asymmetric.

Las posiciones que abre esta estrategia quedan marcadas
`Position.strategy_tag="scalping"` (ver portfolio/portfolio.py) - esa marca
es la que mantiene esta gestion completamente AISLADA de
weekly_asymmetric/vol_arbitrage: `build_exit_signals()` de aca abajo
UNICAMENTE evalua posiciones con esa marca, nunca la posicion de Octubre
(ni ninguna otra) que gestiona weekly_asymmetric.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from ggal_bot.config import SETTINGS
from ggal_bot.data.iv_mean_reversion import IVMeanReversionTracker
from ggal_bot.data.option_chain import OrderBookSnapshot
from ggal_bot.models.microstructure import passes_min_ask_depth
from ggal_bot.models.volatility_surface import VolatilitySurface
from ggal_bot.portfolio.portfolio import Portfolio
from ggal_bot.risk.risk_manager import RiskManager
from ggal_bot.strategy.weekly_asymmetric import EntryScanDiagnostics, EntrySignal, ExitSignal, WeeklyAsymmetricStrategy


class ScalpingStrategy:
    def __init__(self, risk_manager: RiskManager, config=None):
        self.risk_manager = risk_manager
        self.cfg = config if config is not None else SETTINGS.scalping
        # Escaneo de entradas REUSADO por composicion (ver docstring del
        # modulo) - nunca se llama build_exit_signals()/
        # scan_spread_completion_signals() de esta instancia interna.
        self._entry_scanner = WeeklyAsymmetricStrategy(risk_manager, config=self.cfg)
        self.iv_tracker = IVMeanReversionTracker(
            max_window_seconds=self.cfg.iv_reversion_window_seconds,
            min_samples=self.cfg.iv_reversion_min_samples,
        )
        # Ver EntryScanDiagnostics: se sobreescribe en cada scan_entry_signals()
        # (mismo objeto que produce self._entry_scanner, expuesto aca para
        # que run_bot.py lo loguee igual que hace con el de weekly_asymmetric).
        self.last_scan_diagnostics: Optional[EntryScanDiagnostics] = None

    def scan_entry_signals(
        self,
        surface: VolatilitySurface,
        recent_volumes: Dict[str, float],
        order_books: Dict[str, OrderBookSnapshot],
        trend: str,
        momentum_shift: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> List[EntrySignal]:
        """
        `order_books`: puntas vigentes por simbolo (ej.
        `{q.symbol: q.book for q in valid_quotes}`), necesarias para el
        filtro de profundidad minima de ASK (ver
        ScalpingConfig.enable_min_ask_depth_filter) - `surface.quotes` no
        expone directamente el book, solo lo que VolatilitySurface necesita
        para el smile.

        `now`: se usa UNICAMENTE para alimentar el tracker de reversion de
        IV (ver data/iv_mean_reversion.py) con la marca de tiempo real de
        esta lectura - no afecta el escaneo de entradas en si (que sigue
        siendo delegado, sin I/O, a WeeklyAsymmetricStrategy.scan_entry_signals).
        """
        # Alimenta el tracker de reversion de IV con TODAS las cotizaciones
        # del scan (no solo las que terminan calificando como señal), para
        # que la salida por reversion (ver build_exit_signals) tenga
        # historia incluso de bases donde el bot ya tiene una posicion
        # abierta pero que ya no vuelven a aparecer como EntrySignal nuevo.
        for q in surface.quotes:
            dislocation = surface.smile_dislocation(q)
            self.iv_tracker.update(q.symbol, dislocation, now=now)

        candidates = self._entry_scanner.scan_entry_signals(
            surface, recent_volumes, trend=trend, momentum_shift=momentum_shift,
        )
        self.last_scan_diagnostics = self._entry_scanner.last_scan_diagnostics

        if not self.cfg.enable_min_ask_depth_filter:
            return candidates

        filtered: List[EntrySignal] = []
        for candidate in candidates:
            book = order_books.get(candidate.symbol)
            if book is None or not passes_min_ask_depth(book, self.cfg.min_ask_size_for_entry):
                continue  # profundidad de ASK insuficiente para garantizar fill inmediato (ver microstructure.py)
            filtered.append(candidate)
        return filtered

    def build_exit_signals(
        self, portfolio: Portfolio, current_prices: Dict[str, float], now: datetime,
    ) -> List[ExitSignal]:
        """
        Glue hacia risk.risk_manager.RiskManager.evaluate_scalping_exit()
        (unica fuente de verdad de "cuando cerrar" bajo este modo, mismo
        criterio que weekly_asymmetric.build_exit_signals() con
        evaluate_position_exit) mas la salida adicional por reversion de IV
        (ver data/iv_mean_reversion.py) - complementa, no reemplaza, a la
        de RiskManager.

        UNICAMENTE evalua posiciones con `strategy_tag == "scalping"` (ver
        portfolio.Position.strategy_tag) - nunca toca una posicion de
        weekly_asymmetric/vol_arbitrage, ni siquiera si comparten simbolo
        (no deberia pasar: el bot no permite dos posiciones abiertas sobre
        la misma base a la vez, ver run_bot.py:_position_quantity).
        """
        cfg = self.cfg
        signals: List[ExitSignal] = []
        for position in portfolio.positions:
            if position.strategy_tag != "scalping":
                continue
            if position.quantity <= 0:
                continue  # long-only: no hay pata corta propia que gestionar aca
            if position.entry_price is None or position.entry_time is None or position.expiry is None:
                continue  # posicion sin metadata de entrada: no se puede evaluar Stop Loss/Take Profit/horizonte

            current_price = current_prices.get(position.symbol)
            reason = self.risk_manager.evaluate_scalping_exit(
                entry_price=position.entry_price, current_price=current_price,
                entry_time=position.entry_time, now=now, expiry=position.expiry,
                stop_loss_pct=cfg.stop_loss_pct, take_profit_pct=cfg.take_profit_pct,
                max_holding_minutes=cfg.max_holding_minutes,
                min_progress_pnl_pct=cfg.min_progress_pnl_pct,
                progress_check_minutes=cfg.progress_check_minutes,
                eod_close_enabled=cfg.eod_close_enabled,
                eod_close_time=cfg.eod_close_time,
                eod_timezone_offset_hours=cfg.eod_timezone_offset_hours,
            )

            if reason is None and cfg.enable_iv_mean_reversion_exit:
                if self.iv_tracker.has_reverted(position.symbol, exit_abs_zscore=cfg.iv_reversion_exit_zscore):
                    reason = "scalping_iv_mean_reversion"

            if reason is not None:
                signals.append(ExitSignal(symbol=position.symbol, reason=reason, quantity=position.quantity))
        return signals
