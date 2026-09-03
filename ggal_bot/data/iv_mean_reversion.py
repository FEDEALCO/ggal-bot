"""
iv_mean_reversion.py
=======================
Deteccion de REVERSION de la dislocacion de IV (smile) en ALTA FRECUENCIA,
para la salida "iv_mean_reversion" del modo Scalping (ver
config.ScalpingConfig.enable_iv_mean_reversion_exit y
strategy/scalping.py:ScalpingStrategy) - modulo ADITIVO, no afecta a
weekly_asymmetric/vol_arbitrage.

Motivo de un modulo aparte (en vez de una funcion mas en
models/volatility_surface.py): el estado que necesita (una ventana rodante
de muestras POR SIMBOLO) es justamente lo que WeeklyAsymmetricStrategy evita
a proposito (ese modulo es deliberadamente libre de estado / stateless, ver
su docstring, para mantenerlo trivial de testear con datos sinteticos) -
separar este tracker mantiene esa propiedad intacta para el modo Long-First
original, y confina el estado nuevo a Scalping, que sí lo necesita.

Tesis: WeeklyAsymmetricStrategy.scan_entry_signals() ya exige una
dislocacion de smile por debajo de un umbral FIJO
(cfg.smile_threshold_vol_points), medido en puntos de vol ABSOLUTOS. Ese
umbral fijo no distingue "una base que SIEMPRE tiene 2-3 vol pts de ruido
de smile" de "una base que de golpe se desvio muy por fuera de su propio
comportamiento reciente" - la segunda es la candidata mas fuerte a un
scalp de reversion a la media en un horizonte de minutos, la primera es
solo ruido estructural de ese book en particular. Este tracker complementa
ese umbral fijo con un z-score de la dislocacion de CADA simbolo contra su
propia ventana rodante reciente. La salida de esta tesis puntual es,
precisamente, cuando ese z-score vuelve a acercarse a 0: la dislocacion que
motivo la entrada ya se corrigio, la tesis de esa posicion en particular ya
se cumplio (o se agoto), sin esperar a que Stop Loss/Take Profit/horizonte
lo fuercen por otro motivo.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, Optional, Tuple


@dataclass
class _SymbolWindow:
    samples: Deque[Tuple[float, float]] = field(default_factory=deque)  # (timestamp_epoch_seconds, dislocation)


class IVMeanReversionTracker:
    """
    Instanciar UNA por ScalpingStrategy (no compartir con weekly_asymmetric)
    y alimentar con `update()` en cada ciclo de escaneo, para TODAS las
    cotizaciones vistas (no solo las que califican como señal de entrada) -
    ver ScalpingStrategy.scan_entry_signals, que alimenta este tracker
    antes de aplicar ningun filtro.
    """

    def __init__(self, max_window_seconds: float = 1800.0, min_samples: int = 10, max_samples: int = 500):
        self.max_window_seconds = max_window_seconds
        self.min_samples = min_samples
        self.max_samples = max_samples
        self._windows: Dict[str, _SymbolWindow] = {}

    def update(self, symbol: str, dislocation: Optional[float], now: Optional[datetime] = None) -> None:
        """Ausencia de dislocacion (None - ej. IV no calculable este ciclo) no agrega ninguna muestra."""
        if dislocation is None:
            return
        ts = (now if now is not None else datetime.now(timezone.utc)).timestamp()
        window = self._windows.setdefault(symbol, _SymbolWindow())
        window.samples.append((ts, dislocation))
        self._trim(window, ts)

    def _trim(self, window: _SymbolWindow, now_ts: float) -> None:
        while window.samples and (now_ts - window.samples[0][0]) > self.max_window_seconds:
            window.samples.popleft()
        while len(window.samples) > self.max_samples:
            window.samples.popleft()

    def sample_count(self, symbol: str) -> int:
        window = self._windows.get(symbol)
        return len(window.samples) if window is not None else 0

    def zscore(self, symbol: str) -> Optional[float]:
        """
        z-score de la ULTIMA muestra contra la media/desvio de toda la
        ventana rodante vigente para `symbol`. None si todavia no hay
        `min_samples` muestras, o si el desvio es 0 (serie constante - un
        z-score no esta definido, no es un caso de "reversion completa").
        """
        window = self._windows.get(symbol)
        if window is None or len(window.samples) < self.min_samples:
            return None
        values = [v for _, v in window.samples]
        mean = statistics.fmean(values)
        try:
            stdev = statistics.stdev(values)
        except statistics.StatisticsError:
            return None
        if stdev == 0:
            return None
        return (values[-1] - mean) / stdev

    def has_reverted(self, symbol: str, exit_abs_zscore: float) -> bool:
        """
        True si hay historia suficiente Y el |z-score| actual ya volvio a
        estar por DEBAJO de `exit_abs_zscore` (la dislocacion que motivo la
        entrada ya se corrigio hacia el promedio reciente de esta base en
        particular). Sin historia suficiente, devuelve False - ausencia de
        informacion NUNCA fuerza un cierre (mismo criterio que
        risk.risk_manager.RiskManager.evaluate_vega_decay_exit).
        """
        z = self.zscore(symbol)
        if z is None:
            return False
        return abs(z) <= exit_abs_zscore
