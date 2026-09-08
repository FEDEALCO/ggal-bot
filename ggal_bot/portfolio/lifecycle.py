"""
lifecycle.py
=============
Position Lifecycle MINIMO (TANDA 2 "OPTIMIZACION EJECUTABLE", seccion 4,
2026-09-08): reconstruye EPISODIOS (una apertura real -> cero) a partir del
Position Lifecycle Event Journal ya existente (ver portfolio/event_journal.py,
Fase 5.3). Deliberadamente NO implementa MAE/MFE/DTE_at_entry/moneyness_at_
entry/numero de adds-reducciones - eso queda en BACKLOG explicito (ver
reporte de esta tanda) hasta que haga falta y no requiera refactorizar de
nuevo esto. El objetivo unico de este modulo es el que pidio el usuario
literalmente: "saber exactamente cuando empieza y termina una posicion
real", con los 10 campos minimos pedidos.

DISEÑO - por que episode_id == position_id: en este bot, cada Position
nueva se crea UNA sola vez con un position_id fresco (default_factory, ver
portfolio.Position) y las tres Guardas 2 existentes (_act_on_entry_signal/
_act_on_signal/_act_on_spread_completion_signal, run_bot.py) impiden una
segunda entrada sobre la misma base mientras la posicion siga con
quantity!=0 - es decir, por construccion de codigo (verificado por lectura,
no supuesto), cada ciclo completo apertura->cero de una base corresponde a
EXACTAMENTE un position_id, nunca a mas de uno. No hace falta un
identificador de episodio matematicamente distinto todavia: se expone
`episode_id` como su propio campo (igual a `position_id` hoy) para que
ningun llamador futuro (dashboard, otro reporte) dependa de esta
implicacion interna y para dejar el campo ya presente el dia que este bot
soporte pyramideo real (varias entradas sobre la misma base en simultaneo)
y position_id/episode_id necesiten divergir.

POR QUE NO SE AGREGA UNA COLUMNA NUEVA AL EVENT JOURNAL: event_journal.py
ya advierte explicitamente (ver su docstring) sobre el riesgo de romper
pandas.read_csv si se le agrega una columna a un CSV que en produccion ya
puede tener filas con el header viejo. Este modulo reconstruye todo lo que
necesita (incluido el multiplicador, resuelto por simbolo via
dashboard.pnl_engine.multiplier_for_symbol - misma fuente de verdad que ya
usa match_trades_fifo) a partir del schema YA EXISTENTE, sin tocarlo.

LIMITACION EXPLICITA: si el journal no tiene ningun evento ENTRY/ADD para
un position_id (deberia ser imposible por construccion, pero un dato
corrupto o un CSV truncado a mano podria producirlo), `average_entry` y
`realized_pnl` quedan en None/0.0 respectivamente y el nombre del campo se
deja en `data_insufficient_fields` - nunca se fabrica un precio de entrada.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional

# Tipos de evento que representan APERTURA/AMPLIACION vs. REDUCCION/CIERRE
# de una Position (ver event_journal.VALID_EVENT_TYPES). REJECT/CANCEL no
# pertenecen a ningun episodio real (no llegan a tener position_id).
_ENTRY_EVENT_TYPES = ("ENTRY", "ADD")
_EXIT_EVENT_TYPES = ("REDUCE", "PARTIAL_EXIT", "CLOSE")


@dataclass
class PositionEpisode:
    position_id: str
    episode_id: str
    strategy: str
    symbol: str
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]
    initial_quantity: float
    current_quantity: float
    average_entry: Optional[float]
    realized_pnl: float
    close_reason: Optional[str]
    # Campos que no se pudieron calcular con datos reales de este episodio
    # (ver docstring del modulo) - nunca se fabrica un valor, se deja
    # constancia explicita de cual falto.
    data_insufficient_fields: List[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


def build_episode_lifecycles(events: Iterable[Dict]) -> List[PositionEpisode]:
    """
    `events`: filas del event journal (dicts con, como minimo, las claves de
    PositionEventJournal._HEADER), en ORDEN CRONOLOGICO ascendente - si
    vienen de pandas.read_csv(paths.POSITION_EVENTS_LOG), ordenar por
    timestamp_utc antes de llamar a esta funcion (deliberadamente no se
    ordena aca para no imponer pandas como dependencia dura de este
    modulo - ver el mismo criterio de dependencia opcional en
    dashboard/pnl_engine.py).
    """
    from dashboard.pnl_engine import multiplier_for_symbol  # import perezoso: pandas opcional

    by_position: "Dict[str, List[Dict]]" = {}
    order: List[str] = []
    for ev in events:
        pid = str(ev.get("position_id") or "")
        if not pid:
            continue  # REJECT/CANCEL u otra fila sin position_id: no es parte de ningun episodio
        if pid not in by_position:
            by_position[pid] = []
            order.append(pid)
        by_position[pid].append(ev)

    episodes: List[PositionEpisode] = []
    for pid in order:
        rows = by_position[pid]
        entry_rows = [r for r in rows if r.get("event_type") in _ENTRY_EVENT_TYPES]
        exit_rows = [r for r in rows if r.get("event_type") in _EXIT_EVENT_TYPES]
        close_rows = [r for r in rows if r.get("event_type") == "CLOSE"]
        data_insufficient: List[str] = []

        symbol = str(rows[0].get("symbol") or "")
        strategy = str(rows[0].get("strategy_tag") or "") or "weekly_asymmetric"

        opened_at = _parse_ts(entry_rows[0]["timestamp_utc"]) if entry_rows else None
        if opened_at is None:
            data_insufficient.append("opened_at")

        initial_quantity = _to_float(entry_rows[0].get("quantity_after")) if entry_rows else None
        if initial_quantity is None:
            initial_quantity = 0.0
            data_insufficient.append("initial_quantity")

        total_entry_qty = 0.0
        total_entry_cost = 0.0
        for r in entry_rows:
            qd, px = _to_float(r.get("quantity_delta")), _to_float(r.get("price"))
            if qd is None or px is None:
                continue
            total_entry_qty += abs(qd)
            total_entry_cost += abs(qd) * px
        average_entry = (total_entry_cost / total_entry_qty) if total_entry_qty > 0 else None
        if average_entry is None:
            data_insufficient.append("average_entry")

        mult = multiplier_for_symbol(symbol)
        realized_pnl = 0.0
        for r in exit_rows:
            qd, px = _to_float(r.get("quantity_delta")), _to_float(r.get("price"))
            if qd is None or px is None or average_entry is None:
                continue
            realized_pnl += (px - average_entry) * abs(qd) * mult
        if average_entry is None and exit_rows:
            data_insufficient.append("realized_pnl")

        closed_at = _parse_ts(close_rows[-1]["timestamp_utc"]) if close_rows else None
        close_reason = (close_rows[-1].get("reason") or None) if close_rows else None

        last_with_qty = next(
            (r for r in reversed(rows) if _to_float(r.get("quantity_after")) is not None), None,
        )
        current_quantity = _to_float(last_with_qty.get("quantity_after")) if last_with_qty else None
        if current_quantity is None:
            current_quantity = initial_quantity
            data_insufficient.append("current_quantity")

        episodes.append(PositionEpisode(
            position_id=pid, episode_id=pid, strategy=strategy, symbol=symbol,
            opened_at=opened_at, closed_at=closed_at,
            initial_quantity=initial_quantity, current_quantity=current_quantity,
            average_entry=average_entry, realized_pnl=realized_pnl,
            close_reason=close_reason, data_insufficient_fields=data_insufficient,
        ))
    return episodes


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None
