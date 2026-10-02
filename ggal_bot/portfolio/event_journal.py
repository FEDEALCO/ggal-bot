"""
event_journal.py
=================
Position Lifecycle Event Journal (Fase 5.3, ver AUDITORIA_FASE5.3_*.md y
AUDITORIA_FASE5.2B_FORENSIC_REPLAY.md).

QUE ES: un CSV nuevo, append-only (logs/position_events.csv, ver
paths.POSITION_EVENTS_LOG), con UNA fila por evento de lifecycle real de
una Position: ENTRY (se abre un lote nuevo), REDUCE (se descuenta parte de
un lote via _act_on_exit_signal, cierre total), PARTIAL_EXIT (idem, pero
via signal.reason == "partial_profit_take"), CLOSE (un REDUCE/PARTIAL_EXIT
que deja quantity en 0), REJECT (una señal de entrada NO se ejecuto -
Guarda 1/2/3, sizing no operable, book no operable).

POR QUE UN ARCHIVO NUEVO Y NO EXTENDER shadow_trades.csv:
`ShadowAuditLogger` (order_gateway.py) ya tiene un header FIJO de 10
columnas escrito UNA sola vez, la primera vez que el archivo no existe (ver
_ensure_header()). El archivo de produccion YA EXISTE con ese header desde
antes de esta fase (confirmado via shell de Northflank, Fase 5.1). Si se
agregaran columnas nuevas al header en el codigo, las filas VIEJAS del CSV
real seguirian teniendo solo 10 valores mientras el header (que no se
reescribe solo, `_ensure_header` no dispara sobre un archivo no-vacio) diria
otra cosa el dia que se reescriba, o las filas NUEVAS tendrian mas columnas
que las que el header (viejo, sin reescribir) declara -> pandas.read_csv
(dashboard/pnl_engine.py::load_fills) rompe con ParserError y
degrada TODO el pipeline de PnL/reconciliacion a "vacio" (ver el except
generico en load_fills), no solo las filas nuevas. Es un riesgo real e
innecesario: un archivo nuevo, separado, con su propio header, evita esa
clase entera de problema y preserva la garantia explicita del docstring de
ShadowAuditLogger ("nunca se sobreescribe ni se rota") intacta.

LIMITACION EXPLICITA (no resuelta, no inventada): este journal arranca
VACIO en este deploy - no tiene forma de reconstruir retroactivamente los
eventos de ANTES de este cambio (esos solo existen, de forma mas pobre -
sin position_id/contract_key/strategy_tag/episodio -, en shadow_trades.csv
y fueron ya forenseados manualmente en Fase 5.1/5.2/5.2B). A partir de este
deploy, todo evento de lifecycle nuevo queda aca con la identidad completa.
"""
from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from ggal_bot import paths

if TYPE_CHECKING:  # evita el import real en runtime (ver source_name_provider abajo)
    from ggal_bot.data.market_data_source_log import MarketDataSourceLogger

logger = logging.getLogger("ggal_bot.portfolio.event_journal")

# Tipos de evento validos (ver docstring del modulo). Se valida en
# log_event() para detectar, en desarrollo, un typo de event_type antes de
# que llegue a ensuciar el CSV con un valor no reconocido por ningun lector
# futuro (dashboard, reconciliation.py, etc.).
VALID_EVENT_TYPES = (
    "ENTRY", "ADD", "REDUCE", "PARTIAL_EXIT", "CLOSE", "REJECT", "CANCEL",
    # SHADOW_RESET (Tarea #27 item 5, 2026-10-01, a pedido explicito del
    # usuario): cierre administrativo de TODAS las posiciones shadow
    # abiertas al mid vigente, disparado una sola vez con
    # GGAL_BOT_SHADOW_RESET_ON_START=true - ver run_bot.py::
    # _perform_shadow_reset. Se trata como un cierre mas para
    # reconstruct_positions_from_event_journal() (consume el lote FIFO
    # igual que CLOSE), pero queda distinguible por su propio event_type
    # para que el dashboard pueda separar PnL antes/despues del reset.
    "SHADOW_RESET",
)


class PositionEventJournal:
    """
    Analogo a ShadowAuditLogger pero para eventos de LIFECYCLE de Position
    (no de fill crudo de OrderGateway - ver diferencia en el docstring del
    modulo). Un archivo por proyecto, nunca se sobreescribe ni se rota.
    """

    _HEADER = [
        "timestamp_utc", "event_type", "position_id", "contract_key",
        "symbol", "strategy_tag", "side", "quantity_delta", "quantity_after",
        "price", "order_client_id", "reason", "data_unavailable_fields",
    ]

    def __init__(
        self,
        path: Optional[Path] = None,
        source_name_provider: Optional[Callable[[], str]] = None,
        market_data_source_log: Optional["MarketDataSourceLogger"] = None,
    ):
        """
        `source_name_provider`/`market_data_source_log`: MEJORA 2026-10-02
        (Tarea #27/#28 item 3(b), a pedido explicito del usuario) - ver el
        mismo parametro en ShadowAuditLogger.__init__ (execution/
        order_gateway.py) para el diseño completo; aca aplica igual a cada
        evento de lifecycle en vez de a cada fill. Ambos opcionales (default
        None) para no romper ningun caller/test existente.
        """
        self._path = Path(path) if path is not None else paths.POSITION_EVENTS_LOG
        self._lock = threading.Lock()
        self._source_name_provider = source_name_provider
        self._market_data_source_log = market_data_source_log
        self._ensure_header()

    def _ensure_header(self) -> None:
        with self._lock:
            needs_header = (not self._path.exists()) or self._path.stat().st_size == 0
            if needs_header:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self._HEADER)

    def _log_active_source(self, context: str, correlation_id: str) -> None:
        if self._source_name_provider is None or self._market_data_source_log is None:
            return
        try:
            source_name = self._source_name_provider()
        except Exception as exc:  # noqa: BLE001 - nunca debe tumbar el evento real ya registrado
            logger.warning("PositionEventJournal: source_name_provider() lanzo una excepcion (%s); se omite el registro de fuente.", exc)
            return
        self._market_data_source_log.log_source(context, correlation_id, source_name)

    def log_event(
        self,
        event_type: str,
        *,
        position_id: str = "",
        contract_key: Optional[str] = None,
        symbol: str = "",
        strategy_tag: Optional[str] = None,
        side: str = "",
        quantity_delta: Optional[float] = None,
        quantity_after: Optional[float] = None,
        price: Optional[float] = None,
        order_client_id: str = "",
        reason: str = "",
        data_unavailable_fields: Iterable[str] = (),
    ) -> None:
        """
        Escribe una fila. `data_unavailable_fields`: nombres de los campos
        de ESTA fila que no se pudieron calcular con datos reales (ej.
        "contract_key" si la Position no tiene expiry poblado) - nunca se
        fabrica un valor para evitar dejar esta lista vacia; se deja el
        campo en blanco y su nombre queda ahi, explicito.
        """
        if event_type not in VALID_EVENT_TYPES:
            logger.warning(
                "PositionEventJournal.log_event: event_type=%r no es uno de los tipos "
                "validos (%s) - se registra igual, para no perder el evento, pero revisar "
                "el llamador.", event_type, ", ".join(VALID_EVENT_TYPES),
            )
        self._write_row([
            datetime.now(timezone.utc).isoformat(),
            event_type,
            position_id,
            contract_key or "",
            symbol,
            strategy_tag or "",
            side,
            "" if quantity_delta is None else quantity_delta,
            "" if quantity_after is None else quantity_after,
            "" if price is None else price,
            order_client_id,
            reason,
            ",".join(data_unavailable_fields),
        ])
        self._log_active_source(event_type, position_id or order_client_id)

    def _write_row(self, row) -> None:
        with self._lock:
            try:
                with open(self._path, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(row)
            except Exception:
                # Igual que ShadowAuditLogger._write_row: un fallo de disco
                # al auditar NUNCA debe tumbar una decision de trading real
                # ya tomada (la orden ya se mando/lleno). Se loguea y sigue.
                logger.exception("No se pudo escribir el event journal de lifecycle.")
