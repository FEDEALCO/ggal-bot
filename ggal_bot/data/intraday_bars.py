"""
intraday_bars.py
===================
Agregador de velas intradia (tipicamente 5m/15m) 100% en memoria,
alimentado PUSH-based por el spot que el bot YA esta consumiendo cada ciclo
(no una fuente de datos nueva - ver run_bot.py:_run_scalping_cycle). Modulo
ADITIVO del modo Scalping (ver config.ScalpingConfig), sin ningun efecto
sobre weekly_asymmetric/vol_arbitrage.

Motivo: ninguna fuente disponible para este proyecto (data912.com,
IOL/BrokerRestSource) expone velas intradia de GGAL - solo velas DIARIAS
(ver data/technical_analysis.py:Data912DailyBarsSource,
/historical/stocks/{ticker}) y puntas en vivo tick-by-tick (bid/ask/last).
Se resuelve construyendo las velas intradia LOCALMENTE a partir del mismo
spot que ya llega en cada ciclo de ~2s del bot (alcanza de sobra para
buckets de 5-15 minutos, no hace falta tick-by-tick real de mercado).

Reutiliza el dataclass `DailyBar` y `compute_technical_snapshot()` de
data/technical_analysis.py SIN MODIFICARLOS: ninguna de las dos piezas
depende de que `bar_date` sea unico dentro de la lista de barras -
compute_technical_snapshot() y los indicadores puros (ema/rsi/macd/adx) solo
consumen el ORDEN de la lista (via `[b.close for b in bars]`, etc.), nunca
agrupan ni ordenan por `bar_date`. Por eso una barra intradia puede reusar
`DailyBar` con `bar_date` = la fecha de calendario del bucket (repetida
entre varias barras del mismo dia de rueda) sin romper ningun calculo aguas
abajo - se documenta explicitamente aca porque es una decision de diseño
DELIBERADA, no un descuido. IMPORTANTE: este modulo nunca pasa por
`Data912DailyBarsSource` ni por su cache/de-duplicacion por fecha (ver ese
modulo) - es un buffer local independiente, alimentado unicamente por
`IntradayBarAggregator.on_tick()`.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, List, Optional

from ggal_bot.config import SETTINGS
from ggal_bot.data.technical_analysis import DailyBar, TechnicalSnapshot, Trend, compute_technical_snapshot

logger = logging.getLogger("ggal_bot.intraday_bars")


class IntradayBarAggregator:
    """
    Arma velas OHLC de `interval_minutes` a partir de un stream de
    (timestamp, precio[, volumen]). Un "tick" por ciclo del bot alcanza
    (no hace falta tick-by-tick real de mercado): la cadencia de ~2-5s del
    ciclo entra comoda dentro de un bucket de 5+ minutos.
    """

    def __init__(self, interval_minutes: int, max_bars_retained: int = 500):
        self.interval_minutes = max(1, int(interval_minutes))
        self.max_bars_retained = max(1, int(max_bars_retained))
        self._bars: Deque[DailyBar] = deque(maxlen=self.max_bars_retained)
        self._current_bucket_start: Optional[datetime] = None
        self._current: Optional[DailyBar] = None

    def _floor_to_bucket(self, ts: datetime) -> datetime:
        ts_utc = ts.astimezone(timezone.utc) if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        floored_minute = (ts_utc.minute // self.interval_minutes) * self.interval_minutes
        return ts_utc.replace(minute=floored_minute, second=0, microsecond=0)

    def on_tick(self, ts: datetime, price: Optional[float], volume: float = 0.0) -> None:
        """Ticks con precio invalido (None/<=0) o con timestamp anterior al bucket vigente (reloj/replay fuera de orden) se ignoran en silencio."""
        if price is None or price <= 0:
            return
        bucket_start = self._floor_to_bucket(ts)
        if self._current_bucket_start is None:
            self._open_bucket(bucket_start, price)
        elif bucket_start == self._current_bucket_start:
            self._update_bucket(price, volume)
        elif bucket_start > self._current_bucket_start:
            self._bars.append(self._current)  # type: ignore[arg-type]
            self._open_bucket(bucket_start, price)
        # bucket_start < current_bucket_start: tick fuera de orden - no se reabre una vela ya cerrada.

    def _open_bucket(self, bucket_start: datetime, price: float) -> None:
        self._current_bucket_start = bucket_start
        self._current = DailyBar(
            bar_date=bucket_start.date(), open=price, high=price, low=price, close=price, volume=0.0,
        )

    def _update_bucket(self, price: float, volume: float) -> None:
        bar = self._current
        assert bar is not None
        bar.high = max(bar.high, price)
        bar.low = min(bar.low, price)
        bar.close = price
        bar.volume += max(0.0, volume)

    def bars(self) -> List[DailyBar]:
        """
        Barras CERRADAS, en orden cronologico, mas la barra EN CURSO al
        final (con su `close` = ultimo precio conocido) - se incluye la
        parcial para que la lectura de tendencia no tenga que esperar a que
        se cierre un bucket completo para reaccionar (a costa de que el
        ultimo valor de la serie pueda todavia moverse dentro del bucket
        vigente; los indicadores ya calculados sobre barras previas no se
        recalculan retroactivamente por esto - mismo tipo de trade-off que
        cualquier indicador sobre una vela "en formacion").
        """
        out = list(self._bars)
        if self._current is not None:
            out.append(self._current)
        return out

    def bar_count(self) -> int:
        return len(self._bars) + (1 if self._current is not None else 0)


@dataclass
class MultiTimeframeSnapshot:
    fast: TechnicalSnapshot
    slow: TechnicalSnapshot
    combined_trend: str
    require_agreement: bool


class MultiTimeframeIntradayEngine:
    """
    Dos `IntradayBarAggregator` (rapido/lento - ver
    ScalpingConfig.fast_bar_interval_minutes/slow_bar_interval_minutes,
    tipicamente 5m/15m), alimentados con el MISMO tick, cada uno calculando
    su propio `TechnicalSnapshot` via `compute_technical_snapshot()`
    (reusada tal cual de data/technical_analysis.py, sin fork - mismos
    EMA/RSI/MACD/ADX que el filtro diario, solo con periodos mas cortos y
    velas intradia en vez de diarias).

    `combined_trend` exige, por defecto (`require_multi_timeframe_agreement`),
    que AMBOS timeframes coincidan (confirmacion multi-timeframe) antes de
    habilitar una direccion - si no coinciden, NEUTRAL (el estado mas
    conservador, mismo criterio que el resto del bot: la ausencia de
    confirmacion nunca fuerza una direccion).
    """

    def __init__(self, config=None):
        self.cfg = config if config is not None else SETTINGS.scalping
        self._fast = IntradayBarAggregator(self.cfg.fast_bar_interval_minutes, self.cfg.max_bars_retained)
        self._slow = IntradayBarAggregator(self.cfg.slow_bar_interval_minutes, self.cfg.max_bars_retained)
        self._last_snapshot: Optional[MultiTimeframeSnapshot] = None
        self._last_refresh_at: Optional[datetime] = None

    def on_tick(self, ts: datetime, price: Optional[float], volume: float = 0.0) -> None:
        self._fast.on_tick(ts, price, volume)
        self._slow.on_tick(ts, price, volume)

    def refresh(self, now: Optional[datetime] = None, force: bool = False) -> MultiTimeframeSnapshot:
        """
        Recalcula ambos timeframes si el cache vencio (o `force=True`); si
        no, devuelve la ultima lectura sin recalcular - mismo patron de
        cache que TechnicalAnalysisEngine.refresh(), pero con un intervalo
        propio mucho mas corto (ScalpingConfig.refresh_interval_seconds,
        default 30s en vez de 1h: las velas intradia SI cambian dentro de
        la sesion, a diferencia de las diarias).
        """
        now = now if now is not None else datetime.now(timezone.utc)
        if not force and self._last_snapshot is not None and self._last_refresh_at is not None:
            elapsed = (now - self._last_refresh_at).total_seconds()
            if elapsed < self.cfg.refresh_interval_seconds:
                return self._last_snapshot

        fast_snapshot = compute_technical_snapshot(self._fast.bars(), self.cfg, data_source="intraday_fast")
        slow_snapshot = compute_technical_snapshot(self._slow.bars(), self.cfg, data_source="intraday_slow")

        if self.cfg.require_multi_timeframe_agreement:
            combined = fast_snapshot.trend.value if fast_snapshot.trend == slow_snapshot.trend else Trend.NEUTRAL.value
        else:
            combined = fast_snapshot.trend.value

        snapshot = MultiTimeframeSnapshot(
            fast=fast_snapshot, slow=slow_snapshot, combined_trend=combined,
            require_agreement=self.cfg.require_multi_timeframe_agreement,
        )
        self._last_snapshot = snapshot
        self._last_refresh_at = now
        return snapshot

    def last_snapshot(self) -> Optional[MultiTimeframeSnapshot]:
        return self._last_snapshot
