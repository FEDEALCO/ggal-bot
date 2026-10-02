"""
market_data_source_log.py
===========================
Logger append-only que registra, para cada fill/cancelacion de
ShadowAuditLogger y cada evento de lifecycle de PositionEventJournal, cual
era la fuente de datos de mercado ACTIVA en ese momento (Tarea #27/#28 item
3(b), a pedido explicito del usuario: "quiero que... la fuente activa
quede registrada en cada fill y evento del journal").

CONTEXTO REAL QUE LO MOTIVA: ShadowConfig.allow_mock_source (ver
ggal_bot/config.py y ggal_bot/data/live_shadow_feed.py) ya impide que
MockReplaySource se instancie fuera de tests/dev sin un flag explicito, y
el market-hours gate (RiskConfig.enforce_market_hours_gate) ya bloquea el
DESPACHO de entradas/salidas/hedges cuando la fuente activa es Mock. Pero
ninguno de los dos deja un registro AUDITABLE, fill por fill y evento por
evento, de que fuente broto cada decision - sin esto, reconstruir despues
"cuantos fills reales se hicieron con datos sinteticos" requeriria cruzar
timestamps contra los logs de texto del proceso (ggal_bot.log), fragil y
dependiente de que no se hayan rotado/perdido.

POR QUE UN ARCHIVO NUEVO Y NO UNA COLUMNA EN shadow_trades.csv/
position_events.csv: EXACTAMENTE el mismo motivo documentado en
ggal_bot/portfolio/event_journal.py y ggal_bot/data/market_snapshot_log.py
- ambos archivos ya existen en produccion con un header FIJO escrito una
sola vez (_ensure_header() no se dispara sobre un archivo no vacio).
Agregar una columna nueva al header en el codigo dejaria las filas VIEJAS
del CSV real con menos campos que el header (si se reescribe) o las filas
NUEVAS con mas columnas que el header viejo (si no se reescribe, el
comportamiento real de _ensure_header) - en cualquier caso,
pandas.read_csv (dashboard/pnl_engine.py::load_fills/load_position_events)
rompe con ParserError y degrada TODO el pipeline de PnL/dashboard a
"vacio" de forma silenciosa (excepcion generica ya documentada en esos
modulos). Un archivo nuevo, separado, evita esa clase entera de problema.

COMO SE CORRELACIONA: una fila por cada llamada a log_fill/log_cancel (via
ShadowAuditLogger) o log_event (via PositionEventJournal), con el mismo
`correlation_id` que esos logs ya escriben en su propia fila
(client_order_id para fills/cancelaciones, position_id para eventos de
lifecycle) - un join por timestamp+correlation_id contra shadow_trades.csv/
position_events.csv reconstruye, para cualquier fila de esos archivos, cual
fue la fuente activa en ese instante exacto.

Wiring (ver ShadowAuditLogger.__init__/PositionEventJournal.__init__ y
run_bot.py::GgalOptionsBot.__init__): ambos loggers reciben un
`source_name_provider` (callable sin argumentos, tipicamente
GgalOptionsBot._active_market_data_source_name) y una instancia compartida
de MarketDataSourceLogger - ambos parametros son opcionales (default None)
para no romper ningun test/caller existente que construya
ShadowAuditLogger/PositionEventJournal directamente sin pasarlos (en ese
caso, simplemente no se registra la fuente - comportamiento identico al de
antes de esta mejora).
"""
from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ggal_bot import paths

logger = logging.getLogger("ggal_bot.data.market_data_source_log")


class MarketDataSourceLogger:
    """
    Analogo a MarketSnapshotLogger/PositionEventJournal/ShadowAuditLogger
    pero para la PROCEDENCIA del dato de mercado detras de cada fill/evento,
    no el fill/evento en si.
    """

    _HEADER = ["timestamp_utc", "context", "correlation_id", "active_source_name"]

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else paths.MARKET_DATA_SOURCE_LOG
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        with self._lock:
            needs_header = (not self._path.exists()) or self._path.stat().st_size == 0
            if needs_header:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self._HEADER)

    def log_source(self, context: str, correlation_id: str, active_source_name: str) -> None:
        """
        `context`: que disparo este registro (ej. "shadow_fill",
        "shadow_cancel", o el event_type de PositionEventJournal como
        "ENTRY"/"REDUCE"). `correlation_id`: client_order_id o position_id,
        segun corresponda - el mismo valor que ya se escribio en la fila
        correlacionada de shadow_trades.csv/position_events.csv.
        """
        self._write_row([
            datetime.now(timezone.utc).isoformat(), context, correlation_id, active_source_name,
        ])

    def _write_row(self, row) -> None:
        with self._lock:
            try:
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(row)
            except Exception:
                # Igual que ShadowAuditLogger/PositionEventJournal/
                # MarketSnapshotLogger: un fallo de disco al auditar NUNCA
                # debe tumbar una decision de trading real ya tomada.
                logger.exception("No se pudo escribir el log de fuente de datos de mercado.")
