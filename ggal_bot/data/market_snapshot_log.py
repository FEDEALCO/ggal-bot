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
vencimiento), esto son miles de filas por dia - crece rapido.

ROTACION DIARIA + COMPRESION (MEJORA 2026-10-08, hallazgo de auditoria, a
pedido explicito del usuario): la version original de este modulo se dejo
deliberadamente append-only/sin rotacion "hasta que el tamaño de archivo se
vuelva un problema real, no una complejidad anticipada sin evidencia de que
haga falta" - medido en produccion, ya lo es (~236.9 MB/dia de crecimiento,
~15 dias para llenar el volumen de logs al ritmo observado el 2026-10-08).
Con `rotate_daily=True` (default, ver RiskConfig.market_snapshot_rotate_daily),
`_ensure_header`/`_write_rows` detectan, ANTES de escribir, si el archivo
"vivo" quedo de un dia UTC anterior al de la escritura en curso (comparando
contra su `mtime`, sin necesidad de leer el contenido) y lo archivan
comprimido con gzip a un sibling `{stem}-{YYYY-MM-DD}{suffix}.gz`, dejando
el archivo viejo INTACTO en su version comprimida (nunca se reescribe ni se
migra, mismo criterio que ShadowAuditLogger con el cambio de schema) antes
de empezar un `market_snapshots.csv` nuevo y vacio para el dia que arranca.
El path "vivo" (`paths.MARKET_SNAPSHOT_LOG`) es SIEMPRE el del dia en curso,
asi que dashboard/data/market_data.py no necesita ningun cambio - sigue
leyendo exactamente el mismo path de siempre, que ahora simplemente nunca
acumula mas de ~1 dia de datos.
"""
from __future__ import annotations

import csv
import gzip
import logging
import shutil
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

    def __init__(self, path: Optional[Path] = None, rotate_daily: Optional[bool] = None):
        """
        `rotate_daily`: si se omite (default), se lee de
        RiskConfig.market_snapshot_rotate_daily (True por default - ver
        docstring del modulo). Parametro explicito disponible para tests y
        para cualquier caller que quiera forzar el comportamiento sin tocar
        SETTINGS global.
        """
        self._path = Path(path) if path is not None else paths.MARKET_SNAPSHOT_LOG
        if rotate_daily is None:
            from ggal_bot.config import SETTINGS  # import tardio: evita ciclo de imports con config.py
            rotate_daily = SETTINGS.risk.market_snapshot_rotate_daily
        self._rotate_daily = rotate_daily
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        with self._lock:
            self._rotate_if_new_day_locked()
            needs_header = (not self._path.exists()) or self._path.stat().st_size == 0
            if needs_header:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self._HEADER)

    def _rotate_if_new_day_locked(self, now: Optional[datetime] = None) -> None:
        """
        Debe llamarse con `self._lock` ya tomado. Si `rotate_daily` esta
        desactivado, o el archivo no existe/esta vacio, no hace nada. Si
        existe y su ULTIMA ESCRITURA (`mtime`, sin necesidad de leer el
        contenido - mas liviano sobre un archivo que puede pesar cientos de
        MB) cae en un dia UTC anterior al de `now`, lo archiva comprimido
        (gzip) a un sibling `{stem}-{YYYY-MM-DD}{suffix}.gz` (la fecha es la
        del archivo VIEJO, no la de hoy) y lo borra, dejando el path "vivo"
        libre para empezar limpio. Un archivo de destino ya existente
        (ej. ya se roto antes en este mismo arranque) NUNCA se sobreescribe
        - se deja el archivo actual sin rotar antes que perder datos.
        """
        if not self._rotate_daily:
            return
        if not self._path.exists() or self._path.stat().st_size == 0:
            return
        current = now if now is not None else datetime.now(timezone.utc)
        mtime = datetime.fromtimestamp(self._path.stat().st_mtime, tz=timezone.utc)
        if mtime.date() >= current.date():
            return
        archive_path = self._path.with_name(f"{self._path.stem}-{mtime.date().isoformat()}{self._path.suffix}.gz")
        if archive_path.exists():
            logger.warning(
                "MarketSnapshotLogger: %s ya existe - no se vuelve a archivar %s (queda sin rotar esta vez).",
                archive_path, self._path,
            )
            return
        try:
            with open(self._path, "rb") as src, gzip.open(archive_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            self._path.unlink()
            logger.info("MarketSnapshotLogger: archivado y comprimido %s -> %s.", self._path, archive_path)
        except Exception:
            # Igual que _write_rows: un fallo al archivar nunca debe tumbar
            # el ciclo de trading real - se sigue escribiendo en el mismo
            # archivo (sin rotar) en vez de perder el logging por completo.
            logger.exception(
                "MarketSnapshotLogger: fallo al archivar/comprimir %s - se sigue escribiendo en el mismo archivo.",
                self._path,
            )

    def log_quotes(self, quotes: Iterable, now: Optional[datetime] = None) -> None:
        """
        `quotes`: iterable de data.option_chain.OptionQuote (tipicamente
        self.option_chain.all_quotes(), ver run_bot.py). Una cotizacion sin
        IV calculada todavia (q.iv is None, ej. recien descubierta este
        mismo ciclo) se registra igual, con iv/griegas en blanco - nunca se
        fabrica un valor, se deja constancia explicita de que faltaba.
        """
        effective_now = now if now is not None else datetime.now(timezone.utc)
        ts = effective_now.isoformat()
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
        self._write_rows(rows, now=effective_now)

    def _write_rows(self, rows, now: Optional[datetime] = None) -> None:
        with self._lock:
            try:
                # Chequeo de rotacion diaria ANTES de cada escritura (no
                # solo en __init__/_ensure_header): un proceso de larga vida
                # que cruza la medianoche UTC en medio de una corrida debe
                # rotar/comprimir el archivo del dia anterior ahora, no
                # recien en el proximo restart. Usa el mismo `now` logico
                # que la fila que se esta por escribir (nunca el reloj real
                # directamente), para que un caller/test pueda simular el
                # paso de medianoche sin depender del reloj de la maquina.
                self._rotate_if_new_day_locked(now=now)
                needs_header = (not self._path.exists()) or self._path.stat().st_size == 0
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if needs_header:
                        writer.writerow(self._HEADER)
                    writer.writerows(rows)
            except Exception:
                # Igual que ShadowAuditLogger/PositionEventJournal: un fallo
                # de disco al auditar NUNCA debe tumbar una decision de
                # trading real ya tomada. Se loguea y sigue.
                logger.exception("No se pudo escribir el snapshot de mercado.")
