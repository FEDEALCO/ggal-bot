"""
market_snapshot_log.py
========================
Logger append-only de la cadena de opciones COMPLETA (spot/IV/griegas/book
por simbolo) en cada ciclo (MEJORA 2026-09-28, a pedido explicito del
usuario: "mejor trader quant... exprime tu capacidad al maximo").

POR QUE ESTO NO EXISTIA: verificado por grep (no asumido) que, hasta esta
mejora, NINGUN modulo del proyecto persistia un historial de la cadena de
opciones/superficie de IV - solo existian PositionEventJournal (eventos de
lifecycle de POSICIONES, Fase 5.3) y ShadowAuditLogger (fills simulados),
ambos a nivel de TRADE, nunca de MERCADO. Sin un historial de mercado,
cualquier ajuste de umbral, de modelo de superficie o de sizing solo se
podia validar desplegando a shadow en produccion y esperando dias a que se
acumularan datos suficientes para un export nuevo - literalmente el ciclo
que se vino repitiendo en este seguimiento. Este archivo (uno por
proyecto, nunca se sobreescribe ni se rota, mismo criterio que
ShadowAuditLogger/PositionEventJournal) es la base para poder backtestear
offline cualquier mejora futura contra datos reales de GGAL.

POR QUE UN ARCHIVO NUEVO Y NO EXTENDER shadow_trades.csv/position_events.csv:
mismo motivo documentado en portfolio/event_journal.py - un header fijo que
ya existe en produccion no se puede extender sin arriesgar romper
pandas.read_csv contra las filas viejas. Ademas, esto es un tipo de dato
completamente distinto (snapshot de MERCADO, no evento de POSICION o
FILL) - cabe naturalmente en su propio archivo.

Volumen esperado: una fila POR COTIZACION VIGENTE en la cadena, por ciclo
(~2-5s en modo real, mas espaciado en shadow segun poll_interval_seconds).
Con un universo tipico de GGAL (unas pocas decenas de bases activas por
vencimiento), esto son miles de filas por dia - crece rapido. Se deja tal
cual (append-only, sin rotacion) para esta primera version: la rotacion/
compactado queda como tarea futura si el tamaño de archivo se vuelve un
problema real, no una complejidad anticipada sin evidencia de que haga
falta.
"""
from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ggal_bot import paths

logger = logging.getLogger("ggal_bot.data.market_snapshot_log")


class MarketSnapshotLogger:
    """
    Analogo a PositionEventJournal/ShadowAuditLogger pero para snapshots de
    MERCADO (cadena de opciones completa), no de posiciones ni de fills.
    """

    _HEADER = [
        "timestamp_utc", "symbol", "option_type", "strike", "expiry",
        "days_calendar", "days_business", "spot_ref", "bid", "ask",
        "bid_size", "ask_size", "iv", "delta", "gamma", "vega", "theta",
    ]

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else paths.MARKET_SNAPSHOT_LOG
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        with self._lock:
            needs_header = (not self._path.exists()) or self._path.stat().st_size == 0
            if needs_header:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self._HEADER)

    def log_quotes(self, quotes: Iterable, now: Optional[datetime] = None) -> None:
        """
        `quotes`: iterable de data.option_chain.OptionQuote (tipicamente
        self.option_chain.all_quotes(), ver run_bot.py). Una cotizacion sin
        IV calculada todavia (q.iv is None, ej. recien descubierta este
        mismo ciclo) se registra igual, con iv/griegas en blanco - nunca se
        fabrica un valor, se deja constancia explicita de que faltaba.
        """
        ts = (now if now is not None else datetime.now(timezone.utc)).isoformat()
        rows = []
        for q in quotes:
            greeks = q.greeks or {}
            option_type_value = getattr(q.option_type, "value", q.option_type)
            rows.append([
                ts, q.symbol, option_type_value, q.strike,
                q.expiry.isoformat() if q.expiry is not None else "",
                q.days_calendar, q.days_business, q.spot_ref,
                q.book.bid, q.book.ask, q.book.bid_size, q.book.ask_size,
                "" if q.iv is None else q.iv,
                "" if not greeks else greeks.get("delta", ""),
                "" if not greeks else greeks.get("gamma", ""),
                "" if not greeks else greeks.get("vega", ""),
                "" if not greeks else greeks.get("theta", ""),
            ])
        if not rows:
            return
        self._write_rows(rows)

    def _write_rows(self, rows) -> None:
        with self._lock:
            try:
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerows(rows)
            except Exception:
                # Igual que ShadowAuditLogger/PositionEventJournal: un fallo
                # de disco al auditar NUNCA debe tumbar una decision de
                # trading real ya tomada. Se loguea y sigue.
                logger.exception("No se pudo escribir el snapshot de mercado.")
