"""
dashboard/data/reconciliation.py
===================================
Panel de reconciliacion (Fase 1, mandato explicito del usuario, 2026-09-30):
"PnL total del dashboard = suma de PnL por estrategia = PnL reconstruido
desde el journal, sin duplicados (chequeo por Position ID). Si no cuadra,
banner rojo con la diferencia."

Este modulo SOLO calcula (dataclasses + funciones puras) - la UI decide
como pintar el banner. Reutiliza ggal_bot/backtest/reconstruct.py como
unica fuente de verdad (via dashboard/data/journal.py) - no reimplementa
ninguna matematica de PnL.

Dos chequeos independientes, ambos deben pasar para "reconciliado":

1. reconcile_closed_trades_by_strategy(): ningun Position ID (trade_id)
   aparece en la lista de mas de una estrategia (el bug historico -
   classify_strategy() etiquetando todo como vol_arbitrage - habria
   producido justamente este tipo de inconsistencia si se hubiera armado
   este chequeo antes).
2. cross_check_partition(): dos recorridos INDEPENDIENTES de las mismas
   filas (reconstruct_lifecycle_trades para cerradas,
   reconstruct_open_positions para abiertas) tienen que coincidir
   exactamente en como particionan cada position_id entre cerrado/
   abierto/datos-incompletos.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ggal_bot.backtest.reconstruct import OpenPosition, Trade

RECONCILIATION_TOLERANCE_ARS = 0.01  # redondeo de punto flotante, no una discrepancia real


@dataclass
class ReconciliationResult:
    total_pnl_by_strategy_ars: Dict[str, float]
    total_pnl_sum_ars: float
    duplicate_position_ids: List[str]
    is_reconciled: bool
    detail: Optional[str]


def reconcile_closed_trades_by_strategy(
    trades_by_strategy: Dict[str, List[Trade]],
) -> ReconciliationResult:
    """
    `trades_by_strategy`: {strategy_tag: [Trade, ...]}, construido
    llamando dashboard.data.journal.get_closed_trades() UNA VEZ POR
    estrategia sobre el MISMO conjunto de filas del journal (filtrando
    `strategies=(tag,)` cada vez).
    """
    seen: Dict[str, str] = {}  # position_id (Trade.trade_id) -> primera estrategia donde aparecio
    duplicates: List[str] = []
    totals: Dict[str, float] = {}

    for strategy, trades in trades_by_strategy.items():
        totals[strategy] = sum(t.pnl_gross_ars for t in trades)
        for t in trades:
            prior = seen.get(t.trade_id)
            if prior is not None and prior != strategy:
                duplicates.append(t.trade_id)
            else:
                seen.setdefault(t.trade_id, strategy)

    total_sum = sum(totals.values())
    is_reconciled = len(duplicates) == 0
    detail = None
    if not is_reconciled:
        shown = ", ".join(duplicates[:10])
        more = " ..." if len(duplicates) > 10 else ""
        detail = f"{len(duplicates)} Position ID aparecen en mas de una estrategia: {shown}{more}"

    return ReconciliationResult(
        total_pnl_by_strategy_ars=totals,
        total_pnl_sum_ars=total_sum,
        duplicate_position_ids=duplicates,
        is_reconciled=is_reconciled,
        detail=detail,
    )


@dataclass
class PartitionCrossCheck:
    total_unique_position_ids: int
    closed_trades_count: int
    closed_incomplete_data_count: int
    open_positions_count: int
    open_incomplete_data_count: int
    is_consistent: bool
    detail: Optional[str]


def cross_check_partition(
    rows: List[dict],
    closed_result: Tuple[List[Trade], int, int],
    open_result: Tuple[List[OpenPosition], int, int],
) -> PartitionCrossCheck:
    """
    `closed_result` = reconstruct_lifecycle_trades(rows, strategies=X)
    `open_result` = reconstruct_open_positions(rows, strategies=X)
    Deben llamarse sobre el MISMO `rows` y el MISMO filtro `strategies`.
    """
    trades, still_open_count, closed_incomplete = closed_result
    open_positions, closed_count_mirror, open_incomplete = open_result

    unique_ids = {str(r.get("position_id") or "") for r in rows if r.get("position_id")}

    left_ok = (len(open_positions) + open_incomplete) == still_open_count
    right_ok = closed_count_mirror == (len(trades) + closed_incomplete)
    is_consistent = left_ok and right_ok

    detail = None
    if not is_consistent:
        detail = (
            f"still_open_count={still_open_count} vs open_positions+incomplete="
            f"{len(open_positions) + open_incomplete}; "
            f"closed_count_mirror={closed_count_mirror} vs trades+incomplete="
            f"{len(trades) + closed_incomplete}"
        )

    return PartitionCrossCheck(
        total_unique_position_ids=len(unique_ids),
        closed_trades_count=len(trades),
        closed_incomplete_data_count=closed_incomplete,
        open_positions_count=len(open_positions),
        open_incomplete_data_count=open_incomplete,
        is_consistent=is_consistent,
        detail=detail,
    )
