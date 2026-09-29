"""
signal_funnel_log.py
======================
Logger append-only del EMBUDO de candidatas de entrada (MEJORA 2026-09-29,
a pedido explicito del usuario: "logger de embudo estructurado: universo
completo de candidatas por ciclo con spread, profundidad, griegas, delta y
que filtros paso cada una" - prioridad de despliegue junto con
market_snapshot_log.py, ver REPORT.md §12.3/§12.5 punto 5).

POR QUE ESTO NO EXISTIA: hasta esta mejora, la UNICA persistencia del scan
de entradas era `_log_entry_scan_diagnostics_if_due()` (run_bot.py) - lineas
de `logger.info()` con un RESUMEN AGREGADO (conteos por filtro), nunca
escritas a un archivo. Eso alcanza para responder "¿cuantas se descartaron
por moneyness este ciclo?" pero no "¿CUALES exactamente, con que spread y
griegas en ese instante, y en que filtro se cortaron?" - la segunda pregunta
es la que hace falta para poder recalibrar un umbral (o diagnosticar un
`market_snapshots.csv` vacio de trades, ver §12.3) sin esperar dias de
shadow a que se acumule evidencia suficiente en logs efimeros.

QUE REGISTRA: un CSV con una fila POR CANDIDATA evaluada en cada
scan_entry_signals() (weekly_asymmetric y/o scalping, ver
strategy/weekly_asymmetric.py::CandidateFunnelRecord) - la cotizacion
completa (spread, profundidad de book, IV, griegas) en el momento de la
evaluacion, mas `blocked_at` (el PRIMER filtro que la descarto, en el mismo
orden secuencial documentado en EntryScanDiagnostics, o vacio si califico).

OPT-IN, sin costo cuando esta apagado: `LongFirstConfig`/`ScalpingConfig.
enable_signal_funnel_log` (default False) es lo que decide si
scan_entry_signals() siquiera INSTANCIA los registros (ver
CandidateFunnelRecord) - con el flag apagado, `candidate_funnel` queda
vacio y este logger no tiene nada que escribir. Cuando esta prendido, el
volumen es proporcional al tamaño del universo de opciones por ciclo (unas
pocas decenas de bases activas para GGAL) - crece rapido pero queda
acotado, mismo criterio de "archivo nuevo, append-only, sin rotacion en
esta primera version" que MarketSnapshotLogger.

POR QUE UN ARCHIVO NUEVO Y NO EXTENDER shadow_trades.csv/position_events.csv/
market_snapshots.csv: mismo motivo documentado en esos tres modulos - un
header fijo que ya existe en produccion no se puede extender sin arriesgar
romper pandas.read_csv contra las filas viejas, y esto es un tipo de dato
distinto (candidata EVALUADA con su resultado de filtro, no una cotizacion
de mercado sin mas, un fill, ni un evento de posicion).
"""
from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ggal_bot import paths

logger = logging.getLogger("ggal_bot.data.signal_funnel_log")


class SignalFunnelLogger:
    """
    Analogo a MarketSnapshotLogger/PositionEventJournal/ShadowAuditLogger,
    pero para el resultado del embudo de filtros de ENTRADA de cada ciclo.
    """

    _HEADER = [
        "timestamp_utc", "strategy", "symbol", "option_type", "strike", "expiry",
        "days_business", "spot_ref", "bid", "ask", "bid_size", "ask_size",
        "spread_abs", "spread_relative", "iv", "delta", "gamma", "vega", "theta",
        "dislocation_vol_points", "blocked_at",
    ]

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else paths.SIGNAL_FUNNEL_LOG
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        with self._lock:
            needs_header = (not self._path.exists()) or self._path.stat().st_size == 0
            if needs_header:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self._HEADER)

    def log_funnel(self, strategy: str, records: Iterable, now: Optional[datetime] = None) -> None:
        """
        `strategy`: nombre de la estrategia dueña de este scan
        ("weekly_asymmetric"/"scalping") - inyectado por el llamador
        (run_bot.py conoce cual scan produjo `records`, este logger no lo
        adivina).

        `records`: iterable de
        strategy.weekly_asymmetric.CandidateFunnelRecord (tipicamente
        `strategy_instance.last_scan_diagnostics.candidate_funnel`). Vacio
        si `enable_signal_funnel_log` esta apagado (ver docstring del
        modulo) - en ese caso esta funcion no escribe nada.
        """
        ts = (now if now is not None else datetime.now(timezone.utc)).isoformat()
        rows = []
        for r in records:
            rows.append([
                ts, strategy, r.symbol, r.option_type, r.strike,
                r.expiry.isoformat() if r.expiry is not None else "",
                r.days_business,
                "" if r.spot_ref is None else r.spot_ref,
                "" if r.bid is None else r.bid,
                "" if r.ask is None else r.ask,
                "" if r.bid_size is None else r.bid_size,
                "" if r.ask_size is None else r.ask_size,
                "" if r.spread_abs is None else r.spread_abs,
                "" if r.spread_relative is None else r.spread_relative,
                "" if r.iv is None else r.iv,
                "" if r.delta is None else r.delta,
                "" if r.gamma is None else r.gamma,
                "" if r.vega is None else r.vega,
                "" if r.theta is None else r.theta,
                "" if r.dislocation_vol_points is None else r.dislocation_vol_points,
                r.blocked_at or "",
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
                # Mismo criterio que MarketSnapshotLogger/ShadowAuditLogger/
                # PositionEventJournal: un fallo de disco al auditar NUNCA
                # debe tumbar una decision de trading real ya tomada.
                logger.exception("No se pudo escribir el embudo de señales.")
