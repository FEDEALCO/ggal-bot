"""
technical_analysis.py
========================
Modulo de Analisis Tecnico sobre el grafico DIARIO (1D) de la accion GGAL,
usado como filtro direccional obligatorio para el modo Long-First / Weekly
Asymmetric (ver strategy/weekly_asymmetric.py y config.TechnicalAnalysisConfig):
antes de evaluar la compra de opciones o el armado de spreads, el bot
determina la tendencia principal de GGAL en 1D y solo opera en la direccion
consistente con esa lectura.

Piezas de este modulo:
    1. `DailyBar` + fuentes de datos (`Data912DailyBarsSource`,
       `SyntheticDailyBarsSource`): consiguen las ultimas N velas 1D de
       GGAL, con el mismo patron "real con fallback local" que ya usa
       data/live_shadow_feed.py para el feed de shadow trading (real via
       data912.com si esta disponible, sintetico 100% offline si no).
    2. Indicadores puros (`ema`, `rsi`, `macd`, `adx`): funciones de solo
       calculo, sin dependencias externas (ni pandas ni numpy - se sigue la
       misma convencion que models/black_scholes.py, historical_volatility.py
       y volatility_surface.py: listas de Python + math/statistics), para no
       engordar el empaquetado del .exe (ver build_exe.bat) con
       dependencias que solo hacen falta para el dashboard.
    3. `get_daily_trend_signal()` / `compute_technical_snapshot()`: la
       clasificacion BULLISH/BEARISH/NEUTRAL en si, como funcion PURA (recibe
       las velas ya obtenidas, no hace I/O) para que sea trivialmente
       testeable con datos sinteticos deterministicos.
    4. `TechnicalAnalysisEngine`: el objeto con estado que posee run_bot.py,
       encargado de refrescar el historico (con cache por
       `refresh_interval_seconds`, para no pegarle a la red en cada ciclo de
       ~2s del bot) y exponer la ultima lectura de tendencia.

NOTA DE RIESGO: un filtro tecnico BULLISH/BEARISH/NEUTRAL es una lectura de
la ESTRUCTURA reciente de precios (medias moviles, momentum, fuerza de
tendencia) - no una prediccion de hacia donde va a ir GGAL. Sirve para
evitar comprar Puts en medio de una tendencia alcista fuerte (o viceversa),
no para garantizar que la direccion elegida sea la correcta.
"""

from __future__ import annotations

import logging
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover - degradado igual que en live_shadow_feed.py
    _REQUESTS_AVAILABLE = False

from ggal_bot.config import SETTINGS
from ggal_bot.data.http_utils import http_get_json

logger = logging.getLogger("ggal_bot.technical_analysis")


class Trend(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class MomentumShift(Enum):
    """
    Señal ADICIONAL (no reemplaza a Trend) de reversion temprana: mientras
    Trend clasifica la ESTRUCTURA de precio ya establecida (EMA20/EMA50,
    lagging por construccion), MomentumShift mira el MOMENTUM de corto plazo
    (RSI(14), lider respecto de EMA/ADX/MACD) buscando el caso puntual de
    "la tendencia diaria todavia dice BEARISH/BULLISH, pero el momentum ya
    empezo a girar en contra de esa lectura, con fuerza".

    Motivo del pedido (usuario, 2026-08): el filtro de tendencia 1D estricto
    ("solo Calls en BULLISH, solo Puts en BEARISH") es por diseño un filtro
    de ESTRUCTURA ya confirmada - eso significa, por construccion, que
    siempre va a llegar tarde a un cambio de regimen (la EMA20/EMA50 recien
    cruzan DESPUES de varias ruedas moviendose en la nueva direccion). No se
    elimina el filtro (seguia siendo un requisito explicito mandatorio de
    este mismo proyecto) - se agrega esta señal para relajarlo puntualmente,
    solo cuando hay evidencia de momentum genuina de reversion, y solo bajo
    el umbral EXTREMO de dislocacion de smile (el mismo que ya se exige en
    NEUTRAL - ver WeeklyAsymmetricStrategy.scan_entry_signals()), nunca el
    umbral normal.
    """

    EARLY_BULLISH_REVERSAL = "EARLY_BULLISH_REVERSAL"
    EARLY_BEARISH_REVERSAL = "EARLY_BEARISH_REVERSAL"


# ---------------------------------------------------------------------------
# Datos: velas diarias (OHLCV)
# ---------------------------------------------------------------------------

@dataclass
class DailyBar:
    bar_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def _to_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(rec: Dict, keys: Tuple[str, ...]):
    for k in keys:
        if k in rec and rec[k] is not None:
            return rec[k]
    return None


def _parse_bar_date(raw) -> Optional[date]:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, float)):
        # epoch segundos o milisegundos, segun magnitud.
        try:
            ts = raw / 1000.0 if raw > 10_000_000_000 else raw
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(raw, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw[: len(fmt) + 2], fmt).date()
            except ValueError:
                continue
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


def _parse_bar_record(rec: Dict) -> Optional[DailyBar]:
    """
    Parseo defensivo de un registro crudo de velas diarias: se intentan
    varios nombres de campo plausibles (el esquema de data912 usa claves
    cortas tipo 'o'/'h'/'l'/'c'/'v', ver Data912RestSource._to_raw_quote en
    live_shadow_feed.py para el mismo patron en quotes en vivo) en vez de
    asumir un unico esquema rigido - si el proveedor cambia una clave, esto
    degrada a "barra invalida, se descarta" en vez de romper todo el fetch.
    """
    bar_date = _parse_bar_date(_first_present(rec, ("date", "fecha", "d", "t")))
    o = _to_float(_first_present(rec, ("o", "open", "apertura")))
    h = _to_float(_first_present(rec, ("h", "high", "maximo")))
    l = _to_float(_first_present(rec, ("l", "low", "minimo")))
    c = _to_float(_first_present(rec, ("c", "close", "cierre", "px")))
    v = _to_float(_first_present(rec, ("v", "volume", "volumen"))) or 0.0

    if bar_date is None or o is None or h is None or l is None or c is None:
        return None
    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        return None
    return DailyBar(bar_date=bar_date, open=o, high=h, low=l, close=c, volume=v)


class DailyBarsSource:
    """Interfaz minima que implementan las fuentes de velas diarias de abajo."""

    def fetch(self, ticker: str, lookback_bars: int) -> List[DailyBar]:
        raise NotImplementedError

    def is_available(self) -> bool:
        raise NotImplementedError


class Data912DailyBarsSource(DailyBarsSource):
    """
    Historico diario real via data912.com (mismo proveedor sin autenticacion
    que ya usa live_shadow_feed.py para el shadow trading en tiempo real -
    ver ese modulo para el mismo patron de _get()/timeouts). Documentado
    como "educational/hobby data", con cache de ~2hs del lado del proveedor:
    no pensado para reflejar el ultimo tick, si para el grafico diario.
    """

    def __init__(self):
        self._cfg = SETTINGS.shadow

    def _get(self, endpoint: str):
        # Timeout de PARED REAL via http_utils.http_get_json - ver docstring
        # de ese modulo (mismo motivo que Data912RestSource._get() en
        # live_shadow_feed.py: un timeout de `requests` de 5s puede, en la
        # practica, tardar minutos en levantarse en Windows por una
        # resolucion DNS colgada a nivel de sistema operativo).
        if not _REQUESTS_AVAILABLE:
            raise RuntimeError("El paquete 'requests' no esta instalado.")
        url = self._cfg.data912_base_url.rstrip("/") + endpoint
        return http_get_json(url, timeout=self._cfg.request_timeout_seconds)

    def is_available(self) -> bool:
        try:
            bars = self.fetch(SETTINGS.instruments.underlying_symbol, lookback_bars=5)
            return bool(bars)
        except Exception as exc:  # noqa: BLE001 - probe deliberadamente permisivo
            logger.debug("Data912DailyBarsSource.is_available(): probe fallo: %s", exc)
            return False

    def fetch(self, ticker: str, lookback_bars: int) -> List[DailyBar]:
        endpoint = self._cfg.data912_historical_stocks_endpoint_template.format(ticker=ticker)
        raw = self._get(endpoint)

        bars: List[DailyBar] = []
        for rec in raw or []:
            bar = _parse_bar_record(rec)
            if bar is not None:
                bars.append(bar)

        bars.sort(key=lambda b: b.bar_date)
        # de-duplicar por fecha (se queda con la ultima ocurrencia, por si el
        # proveedor repite una rueda con datos revisados)
        by_date: Dict[date, DailyBar] = {b.bar_date: b for b in bars}
        bars = sorted(by_date.values(), key=lambda b: b.bar_date)

        if lookback_bars and len(bars) > lookback_bars:
            bars = bars[-lookback_bars:]
        return bars


class SyntheticDailyBarsSource(DailyBarsSource):
    """
    Generador local de velas diarias (GBM simple + ruido intradiario para
    high/low), 100% offline: usado cuando data912 no responde y como base
    para los tests deterministicos de este modulo (ver validation/
    test_technical_analysis.py). Mismo espiritu que
    live_shadow_feed.MockReplaySource: nunca se presenta como dato real (se
    loguea siempre que se esta usando), solo mantiene el bot operable/testeable
    sin conectividad.
    """

    def __init__(self, initial_close: Optional[float] = None, daily_vol: Optional[float] = None,
                 drift_per_day: float = 0.0, seed: Optional[int] = None):
        self.initial_close = initial_close if initial_close is not None else SETTINGS.shadow.mock_initial_spot
        # HV anualizada de referencia (ver ShadowConfig.mock_atm_iv) pasada a
        # vol diaria - no pretende ser una estimacion real, solo un ruido
        # con una escala razonable para poder ejercitar los indicadores.
        self.daily_vol = daily_vol if daily_vol is not None else SETTINGS.shadow.mock_atm_iv / math.sqrt(252.0)
        self.drift_per_day = drift_per_day
        self._rng = random.Random(seed) if seed is not None else random.Random()

    def is_available(self) -> bool:
        return True  # generador local, siempre disponible

    def fetch(self, ticker: str, lookback_bars: int) -> List[DailyBar]:  # noqa: ARG002 - ticker no se usa (generador generico)
        n = max(lookback_bars, 1)
        today = datetime.now(timezone.utc).date()

        # Fechas: dias habiles (lunes a viernes) terminando hoy, hacia atras.
        dates: List[date] = []
        cursor = today
        while len(dates) < n:
            if cursor.weekday() < 5:
                dates.append(cursor)
            cursor -= timedelta(days=1)
        dates.reverse()

        bars: List[DailyBar] = []
        close = self.initial_close
        for d in dates:
            prev_close = close
            shock = self._rng.gauss(self.drift_per_day, self.daily_vol)
            close = max(0.01, prev_close * math.exp(shock))
            open_ = prev_close
            intraday_range = abs(self._rng.gauss(0.0, self.daily_vol)) * open_
            high = max(open_, close) + intraday_range
            low = max(0.01, min(open_, close) - intraday_range)
            volume = abs(self._rng.gauss(500_000.0, 150_000.0))
            bars.append(DailyBar(bar_date=d, open=open_, high=high, low=low, close=close, volume=volume))
        return bars


def _resolve_bars_source(cfg) -> Tuple[List[DailyBar], str]:
    """
    Logica "auto" identica en espiritu a LiveShadowFeed: real primero,
    sintetico como fallback, siempre logueando cual se uso. Devuelve
    (barras, nombre_fuente_usada) para que el llamador pueda loguear/
    persistir de donde vinieron.
    """
    ticker = SETTINGS.instruments.underlying_symbol
    source_name = cfg.data_source

    if source_name == "synthetic":
        bars = SyntheticDailyBarsSource().fetch(ticker, cfg.lookback_bars)
        logger.info("TechnicalAnalysisEngine: fuente forzada 'synthetic' (100%% local, %d barras).", len(bars))
        return bars, "synthetic"

    real_source = Data912DailyBarsSource()
    if source_name in ("auto", "data912"):
        try:
            bars = real_source.fetch(ticker, cfg.lookback_bars)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TechnicalAnalysisEngine: fallo al obtener velas diarias reales de data912 (%s).", exc)
            bars = []

        if len(bars) >= cfg.min_bars_required:
            logger.info("TechnicalAnalysisEngine: %d velas 1D reales de %s (data912).", len(bars), ticker)
            return bars, "data912"

        logger.warning(
            "TechnicalAnalysisEngine: data912 devolvio %d velas (< min_bars_required=%d) para %s.",
            len(bars), cfg.min_bars_required, ticker,
        )
        if source_name == "data912":
            # Fuente forzada explicitamente: no se cae a sintetico, se
            # devuelve lo que haya (puede ser insuficiente - lo maneja
            # get_daily_trend_signal() con NEUTRAL + motivo).
            return bars, "data912"

    bars = SyntheticDailyBarsSource().fetch(ticker, cfg.lookback_bars)
    logger.warning(
        "TechnicalAnalysisEngine: se usa el generador synthetic/local (%d barras) - sin datos reales suficientes.",
        len(bars),
    )
    return bars, "synthetic"


# ---------------------------------------------------------------------------
# Indicadores puros (listas alineadas 1:1 con la serie de entrada; None
# donde el indicador todavia no esta definido por falta de historia)
# ---------------------------------------------------------------------------

def ema(values: List[float], period: int) -> List[Optional[float]]:
    """EMA estandar: semilla = SMA de los primeros `period` valores, despues recursivo."""
    n = len(values)
    if period <= 0 or n < period:
        return [None] * n
    k = 2.0 / (period + 1)
    out: List[Optional[float]] = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """RSI de Wilder (suavizado recursivo, no una media movil simple de ganancias/perdidas)."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out

    changes = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(ch, 0.0) for ch in changes]
    losses = [max(-ch, 0.0) for ch in changes]

    def _rsi_value(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_value(avg_gain, avg_loss)
    return out


def macd(
    closes: List[float], fast_period: int = 12, slow_period: int = 26, signal_period: int = 9,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """Devuelve (linea_macd, linea_señal, histograma), las tres alineadas 1:1 con `closes`."""
    n = len(closes)
    ema_fast = ema(closes, fast_period)
    ema_slow = ema(closes, slow_period)
    macd_line: List[Optional[float]] = [
        (a - b) if (a is not None and b is not None) else None for a, b in zip(ema_fast, ema_slow)
    ]

    valid_start = next((i for i, v in enumerate(macd_line) if v is not None), None)
    if valid_start is None:
        return macd_line, [None] * n, [None] * n

    macd_values = [v for v in macd_line[valid_start:]]  # type: ignore[misc]  # ya son todos no-None desde aca
    signal_partial = ema(macd_values, signal_period)
    signal_line: List[Optional[float]] = [None] * valid_start + signal_partial

    histogram: List[Optional[float]] = [
        (m - s) if (m is not None and s is not None) else None for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram


def adx(
    highs: List[float], lows: List[float], closes: List[float], period: int = 14,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """
    ADX de Wilder (+DI/-DI/ADX), las tres listas alineadas 1:1 con
    highs/lows/closes. Requiere al menos 2*period+1 barras para tener un
    primer valor de ADX (period barras para el primer +DM/-DM/TR suavizado,
    period barras mas de DX para el primer promedio suavizado de ADX).
    """
    n = len(highs)
    plus_di: List[Optional[float]] = [None] * n
    minus_di: List[Optional[float]] = [None] * n
    adx_out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return adx_out, plus_di, minus_di

    tr_list: List[float] = [0.0] * n
    plus_dm_list: List[float] = [0.0] * n
    minus_dm_list: List[float] = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm_list[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm_list[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr_list[i] = max(
            highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]),
        )

    def _di_pair(s_tr: float, s_pdm: float, s_mdm: float) -> Tuple[float, float]:
        if s_tr == 0:
            return 0.0, 0.0
        return 100.0 * s_pdm / s_tr, 100.0 * s_mdm / s_tr

    dx_list: List[Optional[float]] = [None] * n
    smoothed_tr = sum(tr_list[1: period + 1])
    smoothed_plus_dm = sum(plus_dm_list[1: period + 1])
    smoothed_minus_dm = sum(minus_dm_list[1: period + 1])

    idx = period
    pdi, mdi = _di_pair(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm)
    plus_di[idx] = pdi
    minus_di[idx] = mdi
    dx_list[idx] = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) != 0 else 0.0

    for i in range(period + 1, n):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm_list[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm_list[i]
        pdi, mdi = _di_pair(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm)
        plus_di[i] = pdi
        minus_di[i] = mdi
        dx_list[i] = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) != 0 else 0.0

    first_dx_idx = period
    last_needed_idx = first_dx_idx + period - 1
    if last_needed_idx >= n:
        return adx_out, plus_di, minus_di

    dx_window = [dx_list[k] for k in range(first_dx_idx, last_needed_idx + 1)]
    adx_val = sum(dx_window) / period  # type: ignore[arg-type]
    adx_out[last_needed_idx] = adx_val
    for i in range(last_needed_idx + 1, n):
        adx_val = (adx_val * (period - 1) + dx_list[i]) / period  # type: ignore[operator]
        adx_out[i] = adx_val

    return adx_out, plus_di, minus_di


# ---------------------------------------------------------------------------
# Clasificacion de tendencia (funcion pura: recibe las barras ya obtenidas)
# ---------------------------------------------------------------------------

@dataclass
class TechnicalSnapshot:
    as_of: Optional[date]
    bars_used: int
    last_close: Optional[float]
    ema_fast: Optional[float]
    ema_slow: Optional[float]
    rsi_value: Optional[float]
    adx_value: Optional[float]
    plus_di: Optional[float]
    minus_di: Optional[float]
    macd_line: Optional[float]
    macd_signal: Optional[float]
    macd_histogram: Optional[float]
    trend: Trend
    reason: str
    data_source: str = "unknown"
    momentum_shift: Optional[str] = None


def compute_technical_snapshot(bars: List[DailyBar], config=None, data_source: str = "unknown") -> TechnicalSnapshot:
    """
    Calcula todos los indicadores y clasifica la tendencia 1D. Funcion PURA
    (sin I/O): recibe las barras ya obtenidas (ver TechnicalAnalysisEngine
    para el fetch+cache), lo que la hace trivial de testear con series
    sinteticas deterministicas (ver validation/test_technical_analysis.py).
    """
    cfg = config if config is not None else SETTINGS.technical_analysis

    def _insufficient(reason: str) -> TechnicalSnapshot:
        return TechnicalSnapshot(
            as_of=bars[-1].bar_date if bars else None,
            bars_used=len(bars),
            last_close=bars[-1].close if bars else None,
            ema_fast=None, ema_slow=None, rsi_value=None, adx_value=None,
            plus_di=None, minus_di=None, macd_line=None, macd_signal=None, macd_histogram=None,
            trend=Trend.NEUTRAL, reason=reason, data_source=data_source,
        )

    if len(bars) < cfg.min_bars_required:
        return _insufficient(
            f"Datos insuficientes: {len(bars)} velas 1D disponibles, se necesitan al menos "
            f"{cfg.min_bars_required} para clasificar tendencia con confianza."
        )

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]

    ema_fast_series = ema(closes, cfg.ema_fast_period)
    ema_slow_series = ema(closes, cfg.ema_slow_period)
    rsi_series = rsi(closes, cfg.rsi_period)
    adx_series, plus_di_series, minus_di_series = adx(highs, lows, closes, cfg.adx_period)
    macd_line_series, macd_signal_series, macd_hist_series = macd(
        closes, cfg.macd_fast_period, cfg.macd_slow_period, cfg.macd_signal_period,
    )

    last_close = closes[-1]
    ema_fast_val = ema_fast_series[-1]
    ema_slow_val = ema_slow_series[-1]
    rsi_val = rsi_series[-1]
    adx_val = adx_series[-1]
    plus_di_val = plus_di_series[-1]
    minus_di_val = minus_di_series[-1]
    macd_line_val = macd_line_series[-1]
    macd_signal_val = macd_signal_series[-1]
    macd_hist_val = macd_hist_series[-1]

    if ema_fast_val is None or ema_slow_val is None or adx_val is None or macd_hist_val is None:
        return _insufficient(
            "Datos insuficientes: no todos los indicadores (EMA/ADX/MACD) tienen historia suficiente todavia."
        )

    bullish = last_close > ema_fast_val > ema_slow_val and adx_val > cfg.adx_trend_threshold and macd_hist_val > 0
    bearish = last_close < ema_fast_val < ema_slow_val and adx_val > cfg.adx_trend_threshold and macd_hist_val < 0

    # -- Momentum Shift / Early Reversal Override (ver MomentumShift arriba) --
    # RSI(14) elegido en vez de, por ejemplo, la pendiente del histograma
    # MACD, por ser adimensional/acotado (0-100): la pendiente de un MACD en
    # precio nominal de GGAL no es comparable entre distintas etapas
    # historicas (la accion viene de un fuerte re-precio nominal por
    # depreciacion del ARS, asi que "puntos de MACD" de hoy no significan lo
    # mismo que hace un año). RSI evita ese problema de escala.
    momentum_shift: Optional[str] = None
    lookback = cfg.momentum_shift_lookback_bars
    if cfg.enable_momentum_shift_override and len(rsi_series) > lookback:
        rsi_now = rsi_series[-1]
        rsi_prior = rsi_series[-1 - lookback]
        if rsi_now is not None and rsi_prior is not None:
            rsi_delta = rsi_now - rsi_prior
            if bearish and rsi_delta >= cfg.momentum_shift_rsi_delta:
                momentum_shift = MomentumShift.EARLY_BULLISH_REVERSAL.value
            elif bullish and -rsi_delta >= cfg.momentum_shift_rsi_delta:
                momentum_shift = MomentumShift.EARLY_BEARISH_REVERSAL.value

    if bullish:
        trend = Trend.BULLISH
        reason = (
            f"Cierre {last_close:.2f} > EMA{cfg.ema_fast_period} {ema_fast_val:.2f} > "
            f"EMA{cfg.ema_slow_period} {ema_slow_val:.2f}; ADX {adx_val:.1f} > {cfg.adx_trend_threshold:.0f}; "
            f"MACD hist {macd_hist_val:+.3f} > 0"
        )
        if momentum_shift:
            reason += f"; MOMENTUM SHIFT: {momentum_shift} (RSI {rsi_prior:.1f} -> {rsi_now:.1f})"
    elif bearish:
        trend = Trend.BEARISH
        reason = (
            f"Cierre {last_close:.2f} < EMA{cfg.ema_fast_period} {ema_fast_val:.2f} < "
            f"EMA{cfg.ema_slow_period} {ema_slow_val:.2f}; ADX {adx_val:.1f} > {cfg.adx_trend_threshold:.0f}; "
            f"MACD hist {macd_hist_val:+.3f} < 0"
        )
        if momentum_shift:
            reason += f"; MOMENTUM SHIFT: {momentum_shift} (RSI {rsi_prior:.1f} -> {rsi_now:.1f})"
    else:
        trend = Trend.NEUTRAL
        reason = (
            f"Estructura lateral o sin fuerza de tendencia clara (Cierre {last_close:.2f}, "
            f"EMA{cfg.ema_fast_period} {ema_fast_val:.2f}, EMA{cfg.ema_slow_period} {ema_slow_val:.2f}, "
            f"ADX {adx_val:.1f}, MACD hist {macd_hist_val:+.3f}): no cumple ni BULLISH ni BEARISH."
        )

    return TechnicalSnapshot(
        as_of=bars[-1].bar_date, bars_used=len(bars), last_close=last_close,
        ema_fast=ema_fast_val, ema_slow=ema_slow_val, rsi_value=rsi_val, adx_value=adx_val,
        plus_di=plus_di_val, minus_di=minus_di_val,
        macd_line=macd_line_val, macd_signal=macd_signal_val, macd_histogram=macd_hist_val,
        trend=trend, reason=reason, data_source=data_source, momentum_shift=momentum_shift,
    )


def get_daily_trend_signal(bars: List[DailyBar], config=None) -> str:
    """
    Punto de entrada literal pedido por el requerimiento funcional: dadas
    las velas 1D, devuelve "BULLISH" | "BEARISH" | "NEUTRAL". Envoltorio
    delgado sobre compute_technical_snapshot() para quien solo necesita el
    string (ver TechnicalAnalysisEngine.get_daily_trend_signal() para la
    variante sin argumentos que usa run_bot.py, con cache incluido).
    """
    return compute_technical_snapshot(bars, config).trend.value


# ---------------------------------------------------------------------------
# Motor con estado: fetch + cache + exposicion de la ultima lectura
# ---------------------------------------------------------------------------

class TechnicalAnalysisEngine:
    """
    Objeto que posee run_bot.py (ver GgalOptionsBot.__init__): encapsula el
    fetch de velas diarias (con cache por `refresh_interval_seconds`, para
    no pegarle a data912 en cada ciclo de ~2s) y expone la ultima lectura de
    tendencia a WeeklyAsymmetricStrategy.
    """

    def __init__(self, config=None):
        self.cfg = config if config is not None else SETTINGS.technical_analysis
        self._last_snapshot: Optional[TechnicalSnapshot] = None
        self._last_refresh_at: Optional[datetime] = None

    def refresh(self, now: Optional[datetime] = None, force: bool = False) -> TechnicalSnapshot:
        """
        Recalcula la tendencia si el cache vencio (o `force=True`); si no,
        devuelve la ultima lectura sin volver a pegarle a la red. `now`
        inyectable (no datetime.now() interno) para que sea testeable de
        forma deterministica, siguiendo el mismo patron que
        risk.risk_manager.RiskManager.evaluate_position_exit().
        """
        now = now if now is not None else datetime.now(timezone.utc)

        if not force and self._last_snapshot is not None and self._last_refresh_at is not None:
            elapsed = (now - self._last_refresh_at).total_seconds()
            if elapsed < self.cfg.refresh_interval_seconds:
                return self._last_snapshot

        bars, source_name = _resolve_bars_source(self.cfg)
        snapshot = compute_technical_snapshot(bars, self.cfg, data_source=source_name)
        self._last_snapshot = snapshot
        self._last_refresh_at = now
        return snapshot

    def get_daily_trend_signal(self) -> str:
        """
        Tendencia cacheada (ver refresh()). Si todavia no se refresco nunca,
        devuelve NEUTRAL (conservador: sin lectura tecnica, no hay filtro
        direccional que aplicar todavia - WeeklyAsymmetricStrategy exige
        entonces una dislocacion extrema, igual que un NEUTRAL real).
        """
        if self._last_snapshot is None:
            return Trend.NEUTRAL.value
        return self._last_snapshot.trend.value

    def last_snapshot(self) -> Optional[TechnicalSnapshot]:
        return self._last_snapshot
