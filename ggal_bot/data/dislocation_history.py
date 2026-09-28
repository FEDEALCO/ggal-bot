"""
dislocation_history.py
========================
Ventana rodante de dislocacion de smile por simbolo + z-score (MEJORA
2026-09-28, a pedido explicito del usuario: "mejor trader quant... exprime
tu capacidad al maximo"). API deliberadamente identica a
data/iv_mean_reversion.py::IVMeanReversionTracker (mismo autor, mismo
patron: `update()` alimentado cada ciclo para TODAS las cotizaciones
vistas, `zscore()` de solo lectura) - esta es una instancia INDEPENDIENTE,
no compartida con la de Scalping, para no mezclar el historial de
dislocacion de un modo con el del otro (cada uno tiene su propia cadencia
de ciclo y su propio universo tipico de vencimientos).

Por que un modulo aparte en vez de reusar IVMeanReversionTracker
directamente: ese modulo esta documentado como confinado a Scalping
("Instanciar UNA por ScalpingStrategy, no compartir con weekly_asymmetric")
para mantener WeeklyAsymmetricStrategy libre de estado (ver su docstring).
Este tracker vive en run_bot.py (GgalOptionsBot._dislocation_tracker,
mismo patron de inyeccion que TechnicalAnalysisEngine para `trend`) y solo
le pasa a WeeklyAsymmetricStrategy.scan_entry_signals() un dict ya
calculado {symbol: zscore} - la estrategia en si sigue sin guardar ningun
estado propio entre llamadas.
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


class DislocationHistoryTracker:
    """
    Instanciar UNA por GgalOptionsBot (weekly_asymmetric) y alimentar con
    `update()` en cada ciclo de escaneo, para TODAS las cotizaciones vistas
    (no solo las que califican como señal de entrada) - mismo criterio que
    IVMeanReversionTracker.
    """

    def __init__(self, max_window_seconds: float = 1800.0, min_samples: int = 10, max_samples: int = 500):
        self.max_window_seconds = max_window_seconds
        self.min_samples = min_samples
        self.max_samples = max_samples
        self._windows: Dict[str, _SymbolWindow] = {}

    def update(self, symbol: str, dislocation: Optional[float], now: Optional[datetime] = None) -> None:
        """Ausencia de dislocacion (None) no agrega ninguna muestra."""
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
        `min_samples` muestras, o si el desvio es 0 (serie constante).
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
