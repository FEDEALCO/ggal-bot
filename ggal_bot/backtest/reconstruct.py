"""
reconstruct.py
================
Reconstruccion de trades cerrados a partir de los DOS exports reales
disponibles (ver conversacion, no hay otro dato historico accesible):

    1. Position Lifecycle Event Journal (export CSV en español, columnas
       "Cuando (UTC), Evento, Ticker, Estrategia, Position ID, Contract Key,
       Lado, Δ Cantidad, Cantidad restante, Precio, Motivo, Campos no
       disponibles") - cubre weekly_asymmetric y scalping.
    2. Reconstruccion de cierres (export CSV, columnas "Ticker, Estrategia,
       Direccion, Cantidad, Entrada, Salida, Precio Entrada, Precio Salida,
       PnL ($), PnL (%), Duracion (s)") - cubre SOLO vol_arbitrage en el
       export disponible (generado por dashboard/pnl_engine.py::match_trades_fifo).

DISEÑO - por que esto NO llama directamente a
ggal_bot.portfolio.lifecycle.build_episode_lifecycles: esa funcion (que
sigue siendo la fuente de verdad para el PnL bruto agregado por episodio,
y cuya formula se replica aca a proposito) colapsa todas las patas de
salida de un episodio en un solo `realized_pnl` agregado, sin exponer el
precio/cantidad de CADA pata individual (cada ENTRY/ADD/PARTIAL_EXIT/CLOSE
es un fill separado con su propia comision+derechos+IVA en la vida real).
Para poder aplicarle el modelo de costos de costs.py con precision por
pata (no solo "una entrada y una salida"), este modulo reconstruye los
episodios con la MISMA matematica que build_episode_lifecycles (promedio
ponderado de entrada, PnL = sum((precio_salida - entrada_promedio) * qty *
multiplicador) por cada pata de salida) pero conservando la lista de patas.

NUNCA fabrica una pata, precio o fecha que no este en el CSV. Un episodio
sin evento CLOSE dentro de la ventana del export queda marcado
`is_still_open=True` y se EXCLUYE de las estadisticas de trades cerrados
(no se le puede calcular un PnL realizado final) - se cuenta y se reporta
aparte, nunca se le inventa un precio de salida.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dashboard.pnl_engine import multiplier_for_symbol

_ENTRY_EVENT_TYPES = ("ENTRY", "ADD")
_EXIT_EVENT_TYPES = ("REDUCE", "PARTIAL_EXIT", "CLOSE")

# Mapeo columna-en-español (tal como las exporta dashboard/app.py, ver
# tab_lifecycle) -> nombre de campo interno (mismas claves que
# ggal_bot.portfolio.event_journal.PositionEventJournal._HEADER).
_LIFECYCLE_COLUMN_MAP = {
    "Cuando (UTC)": "timestamp_utc",
    "Evento": "event_type",
    "Ticker": "symbol",
    "Estrategia": "strategy_tag",
    "Position ID": "position_id",
    "Contract Key": "contract_key",
    "Lado": "side",
    "Δ Cantidad": "quantity_delta",
    "Cantidad restante": "quantity_after",
    "Precio": "price",
    "Motivo": "reason",
    "Campos no disponibles": "data_unavailable_fields",
}


@dataclass
class Leg:
    quantity: float   # siempre positivo (magnitud de la pata)
    price: float
    timestamp: Optional[datetime]


@dataclass
class Trade:
    """
    Una unidad de analisis = UN episodio cerrado (una posicion completa,
    apertura -> cero) o UN trade ya apareado (vol_arbitrage). `entry_legs`/
    `exit_legs` conservan CADA fill por separado, para que costs.py pueda
    cobrar comision+derechos+IVA en cada uno (nunca "una sola entrada y
    salida" cuando en realidad hubo un ADD o un PARTIAL_EXIT de por medio).
    """
    strategy: str
    symbol: str
    trade_id: str
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]
    multiplier: float
    entry_legs: List[Leg]
    exit_legs: List[Leg]
    pnl_gross_ars: float
    close_reason: Optional[str] = None
    contract_key: Optional[str] = None  # "SUBYACENTE|SIMBOLO|YYYY-MM-DD" - solo lifecycle journal
    data_insufficient_fields: List[str] = field(default_factory=list)
    direction: Optional[str] = None  # "long" | "short", normalizado desde "Direccion" (vol_arbitrage) o "Lado" de la ENTRY (lifecycle journal) - ver attribution.attribute_by_option_type_and_direction

    @property
    def entry_notional_ars(self) -> float:
        return sum(leg.quantity * leg.price * self.multiplier for leg in self.entry_legs)

    @property
    def exit_notional_ars(self) -> float:
        return sum(leg.quantity * leg.price * self.multiplier for leg in self.exit_legs)

    @property
    def quantity(self) -> float:
        """Cantidad de entrada total (base para reportar tamaño del trade)."""
        return sum(leg.quantity for leg in self.entry_legs)

    @property
    def holding_seconds(self) -> Optional[float]:
        if self.opened_at is None or self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at).total_seconds()


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


def load_lifecycle_journal_rows(path: Path) -> List[Dict]:
    """
    Lee el export de lifecycle (español) y devuelve filas con las claves
    INTERNAS (ver _LIFECYCLE_COLUMN_MAP), en el mismo orden del archivo -
    el llamador es responsable de que ya venga en orden cronologico
    ascendente (el export lo esta, verificado: primera fila = evento mas
    reciente en algunos exports del dashboard - ORDENAR explicitamente por
    timestamp_utc aca mismo para no depender de eso).
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        raw_rows = list(csv.DictReader(f))
    mapped = []
    for r in raw_rows:
        mapped.append({internal: r.get(spanish, "") for spanish, internal in _LIFECYCLE_COLUMN_MAP.items()})
    mapped.sort(key=lambda r: r["timestamp_utc"])
    return mapped


def reconstruct_lifecycle_trades(
    rows: List[Dict], strategies: Optional[Tuple[str, ...]] = None
) -> Tuple[List[Trade], int, int]:
    """
    Reconstruye Trades cerrados a partir de filas de lifecycle journal (ya
    mapeadas a claves internas, ver load_lifecycle_journal_rows). Replica la
    matematica de ggal_bot.portfolio.lifecycle.build_episode_lifecycles
    (promedio ponderado de entrada; PnL = sum sobre patas de salida de
    (precio_salida - entrada_promedio) * qty * multiplicador), pero
    conserva cada pata individual para poder costear cada una por separado.

    `strategies`: si se pasa, filtra por Estrategia (ej. ("weekly_asymmetric",)) -
    ninguna fila de otra estrategia se mezcla.

    Devuelve (trades_cerrados, cantidad_de_posiciones_todavia_abiertas,
    cantidad_de_posiciones_con_datos_incompletos). Estos son TRES conjuntos
    disjuntos que deben sumar exactamente el total de position_id unicos
    vistos:
      - trades_cerrados: tienen CLOSE y al menos una pata de entrada Y una
        de salida validas (parseables) -> se les calcula PnL/costo.
      - todavia_abiertas: nunca tuvieron un evento CLOSE dentro de la
        ventana del export (la posicion seguia viva al cortar el export).
      - datos_incompletos: SI tuvieron CLOSE (la posicion terminó dentro de
        la ventana) pero falta la pata de ENTRY/ADD correspondiente (o no
        se pudo parsear qty/precio) - tipicamente posiciones abiertas ANTES
        de que empezara la ventana del export ("legacy"). Nunca se fabrica
        un precio/cantidad de entrada para estas: se cuentan y se excluyen
        explicitamente, nunca se descartan en silencio.
    """
    by_position: Dict[str, List[Dict]] = {}
    order: List[str] = []
    for r in rows:
        if strategies is not None and r.get("strategy_tag") not in strategies:
            continue
        pid = str(r.get("position_id") or "")
        if not pid:
            continue  # REJECT/CANCEL: nunca tiene position_id, no es parte de ningun trade
        if pid not in by_position:
            by_position[pid] = []
            order.append(pid)
        by_position[pid].append(r)

    trades: List[Trade] = []
    still_open_count = 0
    incomplete_data_count = 0

    for pid in order:
        pos_rows = by_position[pid]
        entry_rows = [r for r in pos_rows if r.get("event_type") in _ENTRY_EVENT_TYPES]
        exit_rows = [r for r in pos_rows if r.get("event_type") in _EXIT_EVENT_TYPES]
        close_rows = [r for r in pos_rows if r.get("event_type") == "CLOSE"]

        if not close_rows:
            still_open_count += 1
            continue  # nunca se fabrica un cierre - se excluye de las estadisticas de trades cerrados

        symbol = str(pos_rows[0].get("symbol") or "")
        strategy = str(pos_rows[0].get("strategy_tag") or "") or "weekly_asymmetric"
        mult = multiplier_for_symbol(symbol)
        contract_key = str(pos_rows[0].get("contract_key") or "") or None

        entry_legs: List[Leg] = []
        total_entry_qty = 0.0
        total_entry_cost = 0.0
        data_insufficient: List[str] = []
        for r in entry_rows:
            qty, px = _to_float(r.get("quantity_delta")), _to_float(r.get("price"))
            if qty is None or px is None:
                continue
            qty = abs(qty)
            entry_legs.append(Leg(quantity=qty, price=px, timestamp=_parse_ts(r.get("timestamp_utc"))))
            total_entry_qty += qty
            total_entry_cost += qty * px
        average_entry = (total_entry_cost / total_entry_qty) if total_entry_qty > 0 else None
        if average_entry is None:
            data_insufficient.append("average_entry")

        exit_legs: List[Leg] = []
        realized_pnl = 0.0
        for r in exit_rows:
            qty, px = _to_float(r.get("quantity_delta")), _to_float(r.get("price"))
            if qty is None or px is None or average_entry is None:
                continue
            qty = abs(qty)
            exit_legs.append(Leg(quantity=qty, price=px, timestamp=_parse_ts(r.get("timestamp_utc"))))
            realized_pnl += (px - average_entry) * qty * mult
        if average_entry is None and exit_rows:
            data_insufficient.append("realized_pnl")

        opened_at = _parse_ts(entry_rows[0]["timestamp_utc"]) if entry_rows else None
        closed_at = _parse_ts(close_rows[-1]["timestamp_utc"])
        close_reason = close_rows[-1].get("reason") or None
        # "Lado" de la primera pata de ENTRADA real ("buy"/"sell") normalizado
        # a la misma convencion "long"/"short" que usa el export de cierres
        # de vol_arbitrage (columna "Direccion") - ver
        # attribution.attribute_by_option_type_and_direction. None si falta
        # el dato (nunca se fabrica un lado).
        entry_side = str(entry_rows[0].get("side") or "").strip().lower() if entry_rows else ""
        direction = {"buy": "long", "sell": "short"}.get(entry_side)

        if not entry_legs or not exit_legs:
            # SI tuvo CLOSE (termino dentro de la ventana) pero falta la
            # pata de entrada (o de salida) valida - no es un "abierto" (ya
            # cerro) ni un trade costeable (falta un lado). Tipicamente una
            # posicion legacy abierta ANTES de que empezara la ventana del
            # export. Se cuenta explicitamente (nunca se descarta en
            # silencio) para que trades + still_open + incomplete_data
            # sumen exactamente el total de position_id unicos vistos.
            incomplete_data_count += 1
            continue

        trades.append(Trade(
            strategy=strategy, symbol=symbol, trade_id=pid,
            opened_at=opened_at, closed_at=closed_at, multiplier=mult,
            entry_legs=entry_legs, exit_legs=exit_legs,
            pnl_gross_ars=realized_pnl, close_reason=close_reason,
            contract_key=contract_key, data_insufficient_fields=data_insufficient,
            direction=direction,
        ))

    return trades, still_open_count, incomplete_data_count


def load_closed_trades_export(path: Path) -> List[Trade]:
    """
    Lee el export de "reconstruccion de cierres" (español, generado por
    dashboard/pnl_engine.py::match_trades_fifo / closed_trades_to_frame) -
    cada fila YA es un trade cerrado y apareado (una pata de entrada, una
    de salida), asi que se parsea directo sin agrupar por position_id (ese
    concepto no existe para vol_arbitrage - el CSV solo trae
    Ticker/Estrategia/Direccion/Cantidad/Entrada/Salida/Precio Entrada/
    Precio Salida/PnL($)/PnL(%)/Duracion(s)).

    El PnL bruto se RECALCULA desde precio/cantidad/multiplicador (no se
    confia ciegamente en la columna "PnL ($)" del CSV) y se compara contra
    el valor del CSV - si difieren en mas de una tolerancia de redondeo,
    la fila se excluye y se cuenta como inconsistente (nunca se usa un
    numero que no cierra con sus propios insumos).
    """
    trades: List[Trade] = []
    inconsistent = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    for i, r in enumerate(rows):
        symbol = r["Ticker"]
        strategy = r["Estrategia"]
        direction = r["Direccion"]
        qty = _to_float(r["Cantidad"])
        entry_price = _to_float(r["Precio Entrada"])
        exit_price = _to_float(r["Precio Salida"])
        pnl_csv = _to_float(r["PnL ($)"])
        opened_at = _parse_ts(r["Entrada"])
        closed_at = _parse_ts(r["Salida"])
        if qty is None or entry_price is None or exit_price is None or pnl_csv is None:
            inconsistent += 1
            continue

        mult = multiplier_for_symbol(symbol)
        sign = 1.0 if direction == "long" else -1.0
        pnl_recomputed = (exit_price - entry_price) * qty * mult * sign

        # Tolerancia: 1 peso o 0.5% del monto absoluto, lo que sea mayor
        # (redondeo de punto flotante propagado por el propio bot al
        # exportar, no una inconsistencia real de datos).
        tolerance = max(1.0, abs(pnl_csv) * 0.005)
        if abs(pnl_recomputed - pnl_csv) > tolerance:
            inconsistent += 1
            continue

        trades.append(Trade(
            strategy=strategy, symbol=symbol, trade_id=f"vol_arbitrage_{i}",
            opened_at=opened_at, closed_at=closed_at, multiplier=mult,
            entry_legs=[Leg(quantity=qty, price=entry_price, timestamp=opened_at)],
            exit_legs=[Leg(quantity=qty, price=exit_price, timestamp=closed_at)],
            pnl_gross_ars=pnl_csv, direction=(direction or None),
        ))

    if inconsistent:
        import logging
        logging.getLogger("ggal_bot.backtest.reconstruct").warning(
            "%d de %d filas del export de cierres se excluyeron por PnL inconsistente con sus propios "
            "insumos (o campos faltantes) - nunca se uso un valor sin verificar.", inconsistent, len(rows),
        )

    return trades
