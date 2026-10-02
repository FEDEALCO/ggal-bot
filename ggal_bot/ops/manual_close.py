"""
manual_close.py
================
Herramienta ADMINISTRATIVA para cerrar a mano una posicion en modo SHADOW
(GGAL_BOT_SHADOW_MODE=true, ver run_bot.py::GgalOptionsBot.__init__ y el
warning "SHADOW MODE activo... fills simulados en logs/shadow_trades.csv").

CONTEXTO (pregunta del usuario 2026-09-08, "COMO CIERRO MANUALMENTE LAS
OPERACIONES?", sobre las 7 posiciones huerfanas detectadas en produccion -
ver GgalOptionsBot._warn_orphaned_positions_for_active_strategy()):

VERIFICADO por lectura de codigo antes de escribir esto: la produccion
actual corre con GGAL_BOT_SHADOW_MODE=true (el warning de "SHADOW MODE
activo" aparece tal cual en el log de produccion pegado por el usuario).
Bajo shadow mode, run_bot.py NUNCA se conecta a PyRofex ni envia ordenes
reales (ver connect_and_subscribe(): la rama `if self.shadow_mode` nunca
llama a initialize_environment()) - las 7 posiciones huerfanas NO existen
en ninguna cuenta de broker real, existen UNICAMENTE como fills
acumulados en logs/shadow_trades.csv, reconstruidos al arrancar por
ggal_bot/portfolio/reconciliation.py. Por eso "cerrarlas a mano" acá NO es
una operacion financiera real (no compra ni vende nada en un broker) -
es agregar, al mismo CSV que ya usa el bot, un fill de cierre simulado
mas, exactamente con el mismo formato que ShadowAuditLogger.log_fill()
escribe en cada ciclo normal. Si algun dia esto corriera en modo LIVE
(GGAL_BOT_SHADOW_MODE=false) esta herramienta NO aplicaria - ver el punto
1 del "ALCANCE EXPLICITO" en reconciliation.py: ese caso necesitaria
reconciliar contra la cuenta real del broker, un problema distinto.

REGLA DE ORO (evidencia, no fabricacion): esta herramienta NUNCA
adivina un precio de cierre. `price` es un parametro obligatorio que el
que la corre debe completar con el precio de mercado real vigente al
momento de cerrar (ej. el mid actual de la cadena de opciones, o el spot
para el subyacente) - sin eso, el P&L reportado por dashboard/pnl_engine.py
para esa posicion quedaria fabricado.

Uso (linea de comandos, corriendo en el mismo entorno/volumen donde vive
logs/shadow_trades.csv - ej. una shell en el contenedor de Northflank):

    python -m ggal_bot.ops.manual_close GFGC7600OC --price 612.50
    python -m ggal_bot.ops.manual_close "MERV - XMEV - GGAL - 24hs" --price 7050.0

Por defecto detecta automaticamente la cantidad y el lado (compra/venta)
necesarios para dejar la posicion en cero, releyendo logs/shadow_trades.csv
con el mismo algoritmo FIFO ya verificado (dashboard/pnl_engine.py::
match_trades_fifo, reusado tal cual - ver reconciliation.py). Se puede
forzar un cierre parcial con --quantity.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Optional, Tuple

from ggal_bot import paths
from ggal_bot.config import SETTINGS
from ggal_bot.execution.order_gateway import (
    OrderRequest,
    OrderSide,
    OrderTypeEnum,
    ShadowAuditLogger,
)
from ggal_bot.portfolio.event_journal import PositionEventJournal
from ggal_bot.portfolio.reconciliation import (
    ReconciliationUnavailable,
    reconstruct_positions_from_event_journal,
)


def determine_close_order(
    symbol: str,
    csv_path: Optional[Path] = None,
    option_multiplier: Optional[float] = None,
) -> Optional[Tuple[OrderSide, float]]:
    """
    Devuelve (side, quantity) necesarios para dejar `symbol` en cero,
    segun el neto reconstruido de logs/shadow_trades.csv (mismo algoritmo
    FIFO que reconciliation.py, reusado tal cual - ver aggregate_open_
    positions() en dashboard/pnl_engine.py). Devuelve None si el simbolo
    ya esta en cero o nunca tuvo fills.

    Levanta ReconciliationUnavailable si dashboard.pnl_engine no se puede
    importar (pandas/numpy ausentes - ver el mismo motivo documentado en
    reconciliation.py).
    """
    try:
        from dashboard.pnl_engine import (
            aggregate_open_positions,
            load_fills,
            match_trades_fifo,
        )
    except ImportError as exc:
        raise ReconciliationUnavailable(
            "No se pudo importar dashboard.pnl_engine (falta pandas/numpy en este "
            "entorno - ver requirements-dashboard.txt) para determinar la posicion "
            f"neta de {symbol}."
        ) from exc

    path = csv_path if csv_path is not None else paths.SHADOW_TRADES_LOG
    mult = option_multiplier if option_multiplier is not None else SETTINGS.instruments.option_multiplier

    fills = load_fills(path)
    if fills.empty:
        return None

    _closed, open_lots = match_trades_fifo(fills, option_multiplier=mult)
    aggregated = aggregate_open_positions(open_lots)
    row = aggregated[aggregated["symbol"] == symbol]
    if row.empty:
        return None

    qty = float(row["quantity"].iloc[0])
    if qty == 0:
        return None
    side = OrderSide.SELL if qty > 0 else OrderSide.BUY
    return side, abs(qty)


def close_position_manually(
    symbol: str,
    price: float,
    *,
    quantity: Optional[float] = None,
    side: Optional[OrderSide] = None,
    reason: str = "manual_close",
    csv_path: Optional[Path] = None,
    option_multiplier: Optional[float] = None,
) -> Tuple[OrderSide, float]:
    """
    Agrega a logs/shadow_trades.csv (o `csv_path`) un fill de cierre
    simulado para `symbol`, en el mismo formato que ShadowAuditLogger.
    log_fill() escribe en produccion - dashboard/pnl_engine.py::load_fills
    lo va a leer exactamente igual que cualquier otro fill del bot (mismo
    event="shadow_fill"), y el proximo arranque del bot (reconciliation.py)
    va a reconstruir la posicion ya en cero.

    Si no se pasan `quantity`/`side`, se detectan automaticamente con
    determine_close_order() (cierre TOTAL de la posicion neta vigente).
    Pasar ambos permite un cierre PARCIAL explicito.

    `price`: obligatorio, > 0 - el precio de mercado real vigente al
    momento de cerrar (ver docstring del modulo: esta funcion nunca lo
    adivina). `client_order_id` queda prefijado "manual-close-" para que
    quede claro en una auditoria posterior que este fill NO vino del loop
    normal del bot.

    Devuelve (side, quantity) efectivamente registrados.
    """
    if price is None or price <= 0:
        raise ValueError(
            f"{symbol}: price debe ser el precio de mercado real vigente (>0) - "
            "esta funcion nunca fabrica un precio de cierre."
        )

    detected = determine_close_order(symbol, csv_path=csv_path, option_multiplier=option_multiplier)
    if quantity is None or side is None:
        if detected is None:
            raise ValueError(
                f"{symbol}: no se encontro una posicion abierta en "
                f"{csv_path or paths.SHADOW_TRADES_LOG} para cerrar - nada que hacer."
            )
        side, quantity = detected
    if quantity <= 0:
        raise ValueError(f"{symbol}: quantity debe ser > 0 (recibido {quantity!r}).")

    # INVARIANTE 1 (Tarea #27 item 4, ggal_bot/risk/invariants.py): a
    # diferencia de run_bot.py::_act_on_exit_signal (que SIEMPRE recorta de
    # forma segura lote por lote), esta herramienta escribe el fill tal
    # cual se le pide - un --quantity/--side explicito que exceda la
    # posicion neta real dejaria, al proximo reconciliar, una posicion
    # NETA CORTA fabricada por una herramienta manual. Se bloquea.
    if side is OrderSide.SELL:
        available = detected[1] if (detected is not None and detected[0] is OrderSide.SELL) else 0.0
        if quantity > available + 1e-9:
            raise ValueError(
                f"{symbol}: se pidio vender {quantity:g} contratos pero la posicion neta real "
                f"vendible es de solo {available:g} - venderla de mas dejaria una posicion NETA "
                "CORTA fabricada a mano. Si el objetivo es cerrar todo, omiti --quantity (se "
                "detecta automaticamente)."
            )

    audit_logger = ShadowAuditLogger(path=csv_path)
    request = OrderRequest(
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        order_type=OrderTypeEnum.MARKET,
        client_order_id=f"manual-close-{uuid.uuid4().hex[:8]}",
    )
    audit_logger.log_fill(request, fill_price=price, reference_price=price)
    return side, quantity


def determine_close_order_from_journal(
    symbol: str,
    strategy_tag: Optional[str] = None,
    journal_path: Optional[Path] = None,
    option_multiplier: Optional[float] = None,
) -> Optional[Tuple[OrderSide, float, str, Optional[str]]]:
    """
    ACTUALIZACION 2026-10-02 (a pedido explicito del usuario - ver "mira
    manual_close.py" en la conversacion de Tarea #27/#28): equivalente a
    determine_close_order(), pero leyendo logs/position_events.csv (el
    Event Journal) en vez de logs/shadow_trades.csv.

    POR QUE ESTA FUNCION EXISTE (bug real encontrado, no hipotetico):
    desde el 2026-10-01, run_bot.py::_reconcile_portfolio_on_startup YA NO
    usa reconstruct_positions_from_shadow_log() (la funcion que
    determine_close_order() sigue usando) - usa
    reconstruct_positions_from_event_journal() (ver "ACTUALIZACION
    2026-10-01" en reconciliation.py). Las dos fuentes pueden divergir:
    VERIFICADO en produccion (2026-10-02) que determine_close_order()
    reportaba "no se encontro una posicion abierta" para GFGC6800OC
    (qty=7) y GFGV5000OC (qty=-75) porque logs/shadow_trades.csv ya no
    recibe fills de todo lo que pasa por el journal - mientras el journal
    (la fuente que el bot REALMENTE usa para reconciliar al arrancar)
    seguia viendolas abiertas, confirmado por logs/ggal_bot.log mostrando
    7 intentos fallidos de _perform_shadow_reset() sobre esos mismos 2
    simbolos, siempre por falta de cotizacion operable, nunca por no
    encontrarlas.

    `strategy_tag`: si el symbol tiene posiciones abiertas bajo mas de una
    estrategia (weekly_asymmetric/vol_arbitrage/scalping) simultaneamente,
    hay que desambiguar - se levanta ValueError listando las estrategias
    encontradas si se omite y hay mas de una. Pasar "weekly_asymmetric"
    coincide con el default (journal sin strategy_tag o con ese valor
    literal - ver convencion de signo en reconstruct_positions_from_event_
    journal: Position.strategy_tag queda en None para ese caso).

    Devuelve (side, quantity, position_id, contract_key) o None si el
    symbol (+ strategy_tag si se dio) esta en cero o nunca aparecio en el
    journal. `contract_key` puede ser None (no se recupera aca el
    option_chain vigente - no hace falta para cerrar, solo para completar
    griegas, ver reconciliation.py) - se propaga tal cual a la fila CLOSE
    con su nombre en data_unavailable_fields.

    Levanta ReconciliationUnavailable si dashboard.pnl_engine no se puede
    importar (misma dependencia opcional que reconciliation.py).
    """
    positions, _warnings = reconstruct_positions_from_event_journal(
        csv_path=journal_path if journal_path is not None else paths.POSITION_EVENTS_LOG,
        option_multiplier=option_multiplier,
        option_chain=None,
    )
    matches = [p for p in positions if p.symbol == symbol]
    if strategy_tag is not None:
        effective = "weekly_asymmetric" if strategy_tag == "weekly_asymmetric" else strategy_tag
        matches = [p for p in matches if (p.strategy_tag or "weekly_asymmetric") == effective]

    if not matches:
        return None
    if len(matches) > 1:
        found = ", ".join(sorted({p.strategy_tag or "weekly_asymmetric" for p in matches}))
        raise ValueError(
            f"{symbol}: hay posiciones abiertas en mas de una estrategia segun el Event "
            f"Journal ({found}) - especificar --strategy-tag para desambiguar."
        )

    pos = matches[0]
    if abs(pos.quantity) < 1e-9:
        return None
    side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
    return side, abs(pos.quantity), pos.position_id, pos.contract_key


def close_position_manually_from_journal(
    symbol: str,
    price: float,
    *,
    strategy_tag: Optional[str] = None,
    quantity: Optional[float] = None,
    side: Optional[OrderSide] = None,
    reason: str = "manual_close",
    journal_path: Optional[Path] = None,
    shadow_csv_path: Optional[Path] = None,
    option_multiplier: Optional[float] = None,
) -> Tuple[OrderSide, float]:
    """
    Equivalente a close_position_manually(), pero contra el Event Journal
    (ver docstring de determine_close_order_from_journal() para el bug
    real que esto soluciona). Escribe DOS cosas, igual que run_bot.py::
    _perform_shadow_reset (el mismo patron ya establecido para un cierre
    administrativo del bot mismo):

      1. Un evento CLOSE en logs/position_events.csv (PositionEventJournal)
         - esto es LO QUE RECONCILIA el proximo arranque, la parte que
         determine_close_order()/close_position_manually() (shadow-only)
         NO hacian y por eso quedaron obsoletas para este caso.
      2. Un fill simulado en logs/shadow_trades.csv (ShadowAuditLogger,
         igual que antes) - se mantiene por compatibilidad con cualquier
         consumidor que todavia lea ese archivo (ej. dashboard/pnl_engine
         para el calculo de PnL historico).

    `price`: igual que close_position_manually(), obligatorio y nunca
    fabricado - el precio de mercado real vigente al momento de cerrar.

    Devuelve (side, quantity) efectivamente registrados.
    """
    if price is None or price <= 0:
        raise ValueError(
            f"{symbol}: price debe ser el precio de mercado real vigente (>0) - "
            "esta funcion nunca fabrica un precio de cierre."
        )

    detected = determine_close_order_from_journal(
        symbol, strategy_tag=strategy_tag, journal_path=journal_path, option_multiplier=option_multiplier,
    )
    position_id = ""
    contract_key = None
    if quantity is None or side is None:
        if detected is None:
            raise ValueError(
                f"{symbol}: no se encontro una posicion abierta en el Event Journal "
                f"({journal_path or paths.POSITION_EVENTS_LOG}) para cerrar - nada que hacer."
            )
        side, quantity, position_id, contract_key = detected
    elif detected is not None:
        _, _, position_id, contract_key = detected
    if quantity <= 0:
        raise ValueError(f"{symbol}: quantity debe ser > 0 (recibido {quantity!r}).")

    # Mismo invariante que close_position_manually() (ggal_bot/risk/
    # invariants.py, Tarea #27 item 4): un --quantity/--side explicito que
    # exceda la posicion neta real segun el journal dejaria una posicion
    # NETA CORTA fabricada a mano al proximo reconciliar.
    if side is OrderSide.SELL:
        available = detected[1] if (detected is not None and detected[0] is OrderSide.SELL) else 0.0
        if quantity > available + 1e-9:
            raise ValueError(
                f"{symbol}: se pidio vender {quantity:g} contratos pero la posicion neta real "
                f"vendible (segun el Event Journal) es de solo {available:g} - venderla de mas "
                "dejaria una posicion NETA CORTA fabricada a mano. Si el objetivo es cerrar "
                "todo, omiti --quantity (se detecta automaticamente)."
            )

    effective_strategy_tag = strategy_tag if strategy_tag is not None else "weekly_asymmetric"
    quantity_delta = -quantity if side is OrderSide.SELL else quantity
    data_unavailable = [] if contract_key is not None else ["contract_key"]

    journal = PositionEventJournal(path=journal_path)
    journal.log_event(
        "CLOSE",
        position_id=position_id,
        contract_key=contract_key,
        symbol=symbol,
        strategy_tag=effective_strategy_tag,
        side=side.value,
        quantity_delta=quantity_delta,
        quantity_after=0.0,
        price=price,
        order_client_id=f"manual-close-{uuid.uuid4().hex[:8]}",
        reason=reason,
        data_unavailable_fields=data_unavailable,
    )

    audit_logger = ShadowAuditLogger(path=shadow_csv_path)
    request = OrderRequest(
        symbol=symbol, side=side, quantity=quantity, price=price,
        order_type=OrderTypeEnum.MARKET, client_order_id=f"manual-close-{uuid.uuid4().hex[:8]}",
    )
    audit_logger.log_fill(request, fill_price=price, reference_price=price)

    return side, quantity


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Cierra a mano una posicion SHADOW agregando un fill de cierre simulado "
            "a logs/shadow_trades.csv. NUNCA usar en modo LIVE (GGAL_BOT_SHADOW_MODE="
            "false) - ver docstring del modulo."
        )
    )
    parser.add_argument("symbol", help='Simbolo exacto (ej. GFGC7600OC o "MERV - XMEV - GGAL - 24hs")')
    parser.add_argument("--price", type=float, required=True, help="Precio de mercado REAL vigente (obligatorio, nunca se adivina)")
    parser.add_argument("--quantity", type=float, default=None, help="Cantidad a cerrar (default: la posicion neta completa)")
    parser.add_argument("--side", choices=["buy", "sell"], default=None, help="Lado del cierre (default: el opuesto a la posicion neta)")
    parser.add_argument("--reason", default="manual_close")
    parser.add_argument(
        "--from-journal", action="store_true",
        help=(
            "Detectar/cerrar contra logs/position_events.csv (el Event Journal, la fuente que "
            "el bot REALMENTE usa para reconciliar desde 2026-10-01) en vez de "
            "logs/shadow_trades.csv (comportamiento default, obsoleto para este fin desde esa "
            "fecha - ver docstring de determine_close_order_from_journal())."
        ),
    )
    parser.add_argument(
        "--strategy-tag", default=None,
        help="Solo con --from-journal: desambigua si el symbol esta abierto en mas de una estrategia.",
    )
    args = parser.parse_args(argv)

    side = OrderSide(args.side) if args.side else None
    try:
        if args.from_journal:
            used_side, used_qty = close_position_manually_from_journal(
                args.symbol, args.price, strategy_tag=args.strategy_tag,
                quantity=args.quantity, side=side, reason=args.reason,
            )
        else:
            used_side, used_qty = close_position_manually(
                args.symbol, args.price, quantity=args.quantity, side=side, reason=args.reason,
            )
    except (ValueError, ReconciliationUnavailable) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    destino = paths.POSITION_EVENTS_LOG if args.from_journal else paths.SHADOW_TRADES_LOG
    print(
        f"OK - {args.symbol}: registrado cierre {used_side.value} x{used_qty} @ {args.price} "
        f"en {destino}. Reiniciar el bot para que reconciliation.py reconstruya la posicion ya "
        "en cero."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
