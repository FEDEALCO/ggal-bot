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
from ggal_bot.portfolio.reconciliation import ReconciliationUnavailable


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

    if quantity is None or side is None:
        detected = determine_close_order(symbol, csv_path=csv_path, option_multiplier=option_multiplier)
        if detected is None:
            raise ValueError(
                f"{symbol}: no se encontro una posicion abierta en "
                f"{csv_path or paths.SHADOW_TRADES_LOG} para cerrar - nada que hacer."
            )
        side, quantity = detected
    if quantity <= 0:
        raise ValueError(f"{symbol}: quantity debe ser > 0 (recibido {quantity!r}).")

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
    args = parser.parse_args(argv)

    side = OrderSide(args.side) if args.side else None
    try:
        used_side, used_qty = close_position_manually(
            args.symbol, args.price, quantity=args.quantity, side=side, reason=args.reason,
        )
    except (ValueError, ReconciliationUnavailable) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"OK - {args.symbol}: registrado fill de cierre {used_side.value} x{used_qty} "
        f"@ {args.price} en {paths.SHADOW_TRADES_LOG}. Reiniciar el bot para que "
        "reconciliation.py reconstruya la posicion ya en cero."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
