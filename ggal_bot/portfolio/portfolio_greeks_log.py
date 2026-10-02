"""
portfolio_greeks_log.py
=========================
Logger periodico, append-only, de las Griegas agregadas de la cartera -
tanto la CUENTA ENTERA ("portfolio") como el desglose POR ESTRATEGIA
(weekly_asymmetric/vol_arbitrage/scalping) - Tarea #27/#28 item 5, a
pedido explicito del usuario: "Logger periodico de griegas de cartera y
por estrategia, default ON".

POR QUE ESTO NO ES LO MISMO QUE bot_state.json (ggal_bot/state_writer.py):
StateWriter YA publica portfolio_greeks_total/portfolio_greeks_by_expiry en
cada ciclo, pero es un SNAPSHOT VIVO - la escritura es atomica (tmp +
replace) y SOBREESCRIBE el archivo anterior, pensado para que el dashboard
muestre el estado ACTUAL del bot, no para reconstruir como evoluciono en
el tiempo. Tampoco desagrega por strategy_tag (solo por vencimiento). Sin
este logger, responder "como vinieron las griegas de weekly_asymmetric en
las ultimas 2 semanas" o "cuanto delta acumulo scalping antes de que el
limite de RiskConfig.max_delta_total lo frenara" (ver Tarea #27/#28 item 4,
mismo seguimiento) requeriria haber tenido el dashboard abierto todo ese
tiempo tomando capturas - no hay ningun historial persistido.

POR QUE UN ARCHIVO NUEVO Y EN FORMATO "LARGO" (una fila por scope, no una
columna por estrategia): mismo motivo de fondo que
portfolio/event_journal.py/data/market_snapshot_log.py (nunca agregar
columnas a un CSV de produccion con header fijo ya existente), pero ademas
el formato largo (columna `scope` con valores "portfolio"/
"weekly_asymmetric"/"vol_arbitrage"/"scalping"/cualquier strategy_tag
futuro) evita for completo la clase de problema que motiva esa regla: si
algun dia se agrega una estrategia nueva, sus filas simplemente empiezan a
aparecer con un valor de `scope` nuevo - el header nunca necesita cambiar,
nunca hay riesgo de romper pandas.read_csv contra filas viejas.

Default ON (corre SIEMPRE, sin flag - mismo criterio que
MarketSnapshotLogger: a pedido explicito del usuario, sin comportamiento
de trading previo que este cambio pudiera alterar, costo marginal de
escribir unas pocas filas mas por ciclo).
"""
from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

from ggal_bot import paths

logger = logging.getLogger("ggal_bot.portfolio.portfolio_greeks_log")

_GREEK_KEYS = ("delta", "gamma", "vega", "theta")
PORTFOLIO_SCOPE = "portfolio"


class PortfolioGreeksLogger:
    """
    Analogo a MarketSnapshotLogger/SignalFunnelLogger pero para las Griegas
    agregadas, en vez de cotizaciones individuales de la cadena de
    opciones.
    """

    _HEADER = ["timestamp_utc", "scope", "delta", "gamma", "vega", "theta"]

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else paths.PORTFOLIO_GREEKS_LOG
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        with self._lock:
            needs_header = (not self._path.exists()) or self._path.stat().st_size == 0
            if needs_header:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self._HEADER)

    def log_greeks(
        self,
        portfolio_totals: Dict[str, float],
        per_strategy_totals: Dict[str, Dict[str, float]],
        now: Optional[datetime] = None,
    ) -> None:
        """
        `portfolio_totals`: Griegas de TODA la cuenta (tipicamente
        Portfolio.total_greeks()). `per_strategy_totals`: un dict
        {strategy_tag: Griegas} por cada estrategia a registrar
        (tipicamente {tag: portfolio.greeks_for_strategy_tag(tag) for tag
        in VALID_STRATEGIES} - ver run_bot.py). Recibe las Griegas YA
        calculadas (no el Portfolio en si) a proposito, mismo criterio que
        MarketSnapshotLogger.log_quotes(quotes): mantiene este modulo
        desacoplado de portfolio/portfolio.py y facil de testear sin
        construir Position/Portfolio reales.

        Una fila por scope ("portfolio" + una por cada key de
        `per_strategy_totals`, orden alfabetico para que la salida sea
        determinista) - nunca se fabrica un valor: una Griega ausente del
        dict de totales queda en blanco, nunca 0.0 (0.0 es un valor real
        posible, no "sin dato").
        """
        ts = (now if now is not None else datetime.now(timezone.utc)).isoformat()
        rows = [self._row(ts, PORTFOLIO_SCOPE, portfolio_totals)]
        for tag in sorted(per_strategy_totals.keys()):
            rows.append(self._row(ts, tag, per_strategy_totals[tag]))
        self._write_rows(rows)

    @staticmethod
    def _row(ts: str, scope: str, totals: Dict[str, float]) -> list:
        return [ts, scope] + ["" if totals.get(k) is None else totals.get(k) for k in _GREEK_KEYS]

    def _write_rows(self, rows) -> None:
        with self._lock:
            try:
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerows(rows)
            except Exception:
                # Igual que ShadowAuditLogger/PositionEventJournal/
                # MarketSnapshotLogger: un fallo de disco al auditar NUNCA
                # debe tumbar el ciclo de trading real.
                logger.exception("No se pudo escribir el log periodico de Griegas de cartera.")
