"""
ccl_bond_quote_log.py
========================
Logger append-only de cotizaciones RAW (bid/ask/ultimo, sin ningun
calculo derivado) de bonos soberanos - GD30/GD30C/AL30/AL30C por defecto -
via el mismo REST publico data912.com que ya usa Data912RestSource
(ggal_bot/data/live_shadow_feed.py) para la cadena de opciones de GGAL.

MEJORA 2026-09-30 (a pedido explicito del usuario, ver REPORT.md, "Fase 2
- Mercado - CCL implicito/GGAL en USD"): antes de esto, el bot nunca
registraba precio de NINGUN bono - ese panel del dashboard no tenia
NINGUNA fuente de datos propia (SIN DATOS estructural, no un problema de
sincronizacion). Con las cotizaciones crudas de estos 4 tickers logueadas
aca, dashboard/data/ puede calcular CCL = precio_ARS_del_bono /
precio_USD_del_bono_equivalente SIN que el bot tenga que "opinar" sobre
el calculo en si: el bot solo registra los insumos crudos, nunca fabrica
ni publica un CCL ya calculado (mismo principio de "nunca fabricar,
mostrar SIN DATOS si falta un insumo" que rige todo este proyecto).

POR QUE UN ARCHIVO NUEVO Y NO EXTENDER market_snapshots.csv: mismo motivo
documentado en portfolio/event_journal.py/market_snapshot_log.py - un
header fijo que ya existe en produccion no se puede extender sin
arriesgar romper pandas.read_csv contra las filas viejas. Ademas, esto es
un instrumento completamente distinto (bono soberano, no opcion/accion de
GGAL) - cabe naturalmente en su propio archivo.

POR QUE UN POLL HTTP SEPARADO Y NO REUTILIZAR Data912RestSource/
ShadowDataSource: esa clase esta acoplada al universo de instrumentos de
GGAL (bootstrap()/subscribe() filtran explicitamente por
SETTINGS.instruments.underlying_symbol) y a la logica de websocket/poll
del ciclo de TRADING - los bonos no son parte de ese universo ni de
ninguna decision de trading (el bot NUNCA opera bonos), asi que acoplar
esto a esa clase seria una dependencia real sin necesidad, no una
simplificacion. Un poll HTTP standalone (mismo helper http_get_json que
ya usa http_utils.py en todo el proyecto, mismo timeout duro de pared
real documentado ahi) es la opcion mas simple que cumple el pedido.

OPT-IN, apagado por defecto (GGAL_BOT_ENABLE_CCL_BOND_QUOTE_LOG=false,
ver ShadowConfig.enable_ccl_bond_quote_log) - mismo criterio que
MarketSnapshotLogger/SignalFunnelLogger.
"""
from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ggal_bot import paths
from ggal_bot.config import SETTINGS
from ggal_bot.data.http_utils import http_get_json

logger = logging.getLogger("ggal_bot.data.ccl_bond_quote_log")

# Confirmado real via WebFetch a data912.com/live/arg_bonds (2026-09-30):
# los 4 tickers existen con cotizaciones vigentes (GD30~87540 ARS,
# GD30C~54.15 USD, AL30~83950 ARS, AL30C~51.89 USD) - CCL implicito via
# GD30 = px_ARS(GD30) / px_USD(GD30C), analogo para AL30/AL30C.
DEFAULT_BOND_TICKERS: tuple = ("GD30", "GD30C", "AL30", "AL30C")


def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


class CclBondQuoteLogger:
    """Analogo a MarketSnapshotLogger/SignalFunnelLogger pero para cotizaciones de bonos (CCL implicito)."""

    _HEADER = ["timestamp_utc", "symbol", "bid", "ask", "last", "bid_size", "ask_size"]

    def __init__(self, path: Optional[Path] = None, tickers: Iterable[str] = DEFAULT_BOND_TICKERS):
        self._path = Path(path) if path is not None else paths.CCL_BOND_QUOTES_LOG
        self._tickers = tuple(t.upper() for t in tickers)
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        with self._lock:
            needs_header = (not self._path.exists()) or self._path.stat().st_size == 0
            if needs_header:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self._HEADER)

    def fetch_and_log(self, now: Optional[datetime] = None) -> None:
        """
        Un poll HTTP (data912.com /live/arg_bonds) + un append. Nunca
        lanza excepcion (mismo criterio que todos los loggers de este
        proyecto - ver MarketSnapshotLogger/SignalFunnelLogger): un fallo
        de red/parseo se loguea y se omite ESTE poll, nunca tumba el
        ciclo de trading real (que ni siquiera opera este instrumento).
        """
        cfg = SETTINGS.shadow
        try:
            records = http_get_json(
                cfg.data912_base_url.rstrip("/") + cfg.data912_bonds_endpoint,
                timeout=cfg.request_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("CclBondQuoteLogger: fallo al obtener %s de data912 (%s) - se omite este poll.",
                            cfg.data912_bonds_endpoint, exc)
            return

        self.log_records(records, now=now)

    def log_records(self, records: Optional[List[Dict]], now: Optional[datetime] = None) -> None:
        """
        Separado de fetch_and_log() para poder testear el parseo/filtrado
        sin red (records ya obtenidos). NUNCA fabrica una cotizacion
        faltante: un ticker que no aparece en `records` este ciclo se
        omite (no se escribe una fila con blancos) - el panel de
        frescura de datos debe tratar la AUSENCIA de fila reciente como
        SIN DATOS, igual que con cualquier otra fuente.
        """
        by_symbol: Dict[str, dict] = {}
        for rec in records or []:
            sym = str(rec.get("symbol") or "").upper()
            if sym:
                by_symbol[sym] = rec

        ts = (now if now is not None else datetime.now(timezone.utc)).isoformat()
        rows = []
        for ticker in self._tickers:
            rec = by_symbol.get(ticker)
            if rec is None:
                continue
            rows.append([
                ts, ticker,
                _to_float(rec.get("px_bid")), _to_float(rec.get("px_ask")), _to_float(rec.get("c")),
                _to_float(rec.get("q_bid")), _to_float(rec.get("q_ask")),
            ])

        if rows:
            self._write_rows(rows)

    def _write_rows(self, rows: List[list]) -> None:
        with self._lock:
            try:
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerows(rows)
            except Exception:
                logger.exception("No se pudo escribir el log de cotizaciones de bonos (CCL).")
