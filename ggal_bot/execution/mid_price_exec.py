"""
mid_price_exec.py
===================
Motor de ejecucion a mid-price con control de slippage, pensado para las
bases de opciones ilíquidas de BYMA: en vez de cruzar el spread (pagarlo),
coloca la orden límite en el punto medio (bid+ask)/2 y la monitorea con dos
gatillos de cancelacion/repricing independientes:

    1. Timeout: si pasaron `order_timeout_seconds` sin fill, se mejora el
       precio (hasta `max_price_improvements` veces) o se cancela.
    2. Movimiento adverso: si el mid de la propia opcion se desvio mas de
       `max_slippage_pct` desde que se armo la orden, o si el subyacente se
       movio mas de `underlying_move_cancel_pct` desde ese momento, se
       cancela de inmediato (el precio de referencia quedo desactualizado:
       seguir esperando el fill original expone a adverse selection).

Este modulo NO decide *que* operar (eso es responsabilidad de
strategy/vol_arbitrage.py y strategy/delta_hedger.py) - solo *como* insertar
y vigilar la orden una vez que la decision ya fue tomada.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from ggal_bot.config import SETTINGS
from ggal_bot.data.option_chain import OrderBookSnapshot
from ggal_bot.execution.market_making import MarketMakingEngine
from ggal_bot.execution.order_gateway import (
    OrderGateway, OrderRequest, OrderSide, OrderState, OrderStatus, OrderTypeEnum,
)

logger = logging.getLogger("ggal_bot.mid_price_exec")


@dataclass
class _OpenOrderContext:
    """Contexto adicional de una orden en vigilancia, mas alla de lo que trackea OrderGateway."""
    symbol: str
    side: OrderSide
    aggressive: bool
    spot_reference: float           # spot de GGAL al momento de armar la orden
    submitted_at: float = field(default_factory=time.time)


class MidPriceExecutionEngine:
    def __init__(self, order_gateway: OrderGateway, mm_engine: Optional[MarketMakingEngine] = None):
        self.order_gateway = order_gateway
        self.mm_engine = mm_engine or MarketMakingEngine(
            tick_size=SETTINGS.execution.tick_size,
            liquid_spread_relative_threshold=SETTINGS.execution.liquid_spread_relative_threshold,
        )
        self._contexts: Dict[str, _OpenOrderContext] = {}

    # -- Insercion de la orden ----------------------------------------------

    def submit(
        self,
        symbol: str,
        book: OrderBookSnapshot,
        side: OrderSide,
        quantity: float,
        spot_reference: float,
        aggressive: bool = False,
    ) -> OrderState:
        """
        Arma y envia la orden. `aggressive=True` cruza el spread (usar para
        coberturas de delta, donde el riesgo de NO ejecutar pesa mas que el
        spread que se paga); `aggressive=False` (default, usado para las
        señales de arbitraje de volatilidad) cotiza a mid-price para
        capturar spread en vez de pagarlo, segun la liquidez del libro
        (ver MarketMakingEngine.decide_price).
        """
        if aggressive:
            price = book.ask if side is OrderSide.BUY else book.bid
        else:
            price = self.mm_engine.decide_price(book, side.value)

        request = OrderRequest(
            symbol=symbol, side=side, quantity=quantity, price=price,
            order_type=OrderTypeEnum.LIMIT,
        )
        state = self.order_gateway.send(request, reference_price=book.mid)
        self._contexts[request.client_order_id] = _OpenOrderContext(
            symbol=symbol, side=side, aggressive=aggressive, spot_reference=spot_reference,
        )
        logger.info(
            "Orden insertada [%s]: %s %s x%.0f @ %.2f (mid=%.2f, id=%s)",
            "agresiva" if aggressive else "mid-price",
            side.value, symbol, quantity, price, book.mid, request.client_order_id,
        )
        return state

    # -- Vigilancia: timeout + movimiento adverso ---------------------------

    def monitor_and_reprice(
        self,
        current_books: Dict[str, OrderBookSnapshot],
        current_spot: float,
    ) -> None:
        """
        Llamar en cada ciclo del loop principal (ver run_bot.py) para
        revisar todas las ordenes activas de este motor. `current_books`
        debe incluir, como minimo, el book de cada simbolo con una orden
        abierta.
        """
        for client_order_id in list(self._contexts.keys()):
            self._check_one(client_order_id, current_books, current_spot)

    def _check_one(
        self, client_order_id: str, current_books: Dict[str, OrderBookSnapshot], current_spot: float,
    ) -> None:
        state = self.order_gateway.get_state(client_order_id)
        ctx = self._contexts.get(client_order_id)
        if state is None or ctx is None:
            self._contexts.pop(client_order_id, None)
            return

        # Orden ya resuelta (fill/cancel/reject): dejar de vigilarla.
        if state.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self._contexts.pop(client_order_id, None)
            return

        book = current_books.get(ctx.symbol)
        if book is None or book.bid <= 0 or book.ask <= 0:
            return  # sin datos de mercado frescos, no se puede evaluar el gatillo

        # --- Gatillo 1: movimiento adverso -----------------------------
        # Si el mid de la propia opcion se desvio demasiado del precio de
        # referencia con el que se armo la orden, el precio limite quedo
        # "viejo": mantenerlo expone a que alguien lo tome sabiendo mas que
        # nosotros (adverse selection). Se cancela de inmediato, sin esperar
        # el timeout normal.
        if state.reference_price > 0:
            option_drift = abs(book.mid - state.reference_price) / state.reference_price
            if option_drift > SETTINGS.execution.max_slippage_pct:
                logger.warning(
                    "Cancelando %s por slippage de la opcion: %.2f%% > %.2f%% (ref=%.2f, mid actual=%.2f)",
                    client_order_id, option_drift * 100, SETTINGS.execution.max_slippage_pct * 100,
                    state.reference_price, book.mid,
                )
                self._cancel_and_forget(client_order_id)
                return

        # --- Gatillo 2: movimiento del subyacente ----------------------
        # Aunque la opcion en si no se haya movido tanto, un salto del
        # subyacente cambia el delta/skew de referencia bajo el cual se
        # armo la orden: se cancela para re-evaluar con precios frescos en
        # el proximo ciclo de la estrategia, en vez de dejarla expuesta.
        if ctx.spot_reference > 0:
            spot_drift = abs(current_spot - ctx.spot_reference) / ctx.spot_reference
            if spot_drift > SETTINGS.execution.underlying_move_cancel_pct:
                logger.warning(
                    "Cancelando %s por movimiento del subyacente: %.2f%% > %.2f%% (spot ref=%.2f, actual=%.2f)",
                    client_order_id, spot_drift * 100, SETTINGS.execution.underlying_move_cancel_pct * 100,
                    ctx.spot_reference, current_spot,
                )
                self._cancel_and_forget(client_order_id)
                return

        # --- Gatillo 3: timeout de exposicion ---------------------------
        if self.order_gateway.should_reprice(client_order_id):
            self._reprice(client_order_id, state, ctx, book)

    def _reprice(self, client_order_id: str, state: OrderState, ctx: _OpenOrderContext, book: OrderBookSnapshot) -> None:
        self.order_gateway.cancel(client_order_id)
        self._contexts.pop(client_order_id, None)

        if state.price_improvements >= SETTINGS.execution.max_price_improvements:
            logger.warning(
                "Orden %s alcanzo el maximo de mejoras de precio (%d); se cancela sin reintentar.",
                client_order_id, SETTINGS.execution.max_price_improvements,
            )
            return

        # Mejora de precio: si veniamos pasivos (mid-price) y no hubo fill,
        # nos acercamos un tick hacia la punta contraria para aumentar la
        # probabilidad de ejecucion sin llegar a cruzar todo el spread de
        # una sola vez (evita regalar el spread completo ante el primer timeout).
        improved_price = self._improve_price(book, ctx.side)
        new_request = OrderRequest(
            symbol=ctx.symbol, side=ctx.side, quantity=state.request.quantity,
            price=improved_price, order_type=OrderTypeEnum.LIMIT,
        )
        new_state = self.order_gateway.send(new_request, reference_price=book.mid)
        new_state.price_improvements = state.price_improvements + 1
        self._contexts[new_request.client_order_id] = _OpenOrderContext(
            symbol=ctx.symbol, side=ctx.side, aggressive=ctx.aggressive, spot_reference=ctx.spot_reference,
        )
        logger.info(
            "Orden %s re-cotizada como %s: %s x%.0f @ %.2f (mejora #%d)",
            client_order_id, new_request.client_order_id, ctx.side.value,
            state.request.quantity, improved_price, new_state.price_improvements,
        )

    def _improve_price(self, book: OrderBookSnapshot, side: OrderSide) -> float:
        tick = SETTINGS.execution.tick_size
        if side is OrderSide.BUY:
            candidate = self.mm_engine.round_to_tick(book.mid + tick)
            return min(candidate, book.ask)  # nunca mejorar por encima del ask
        candidate = self.mm_engine.round_to_tick(book.mid - tick)
        return max(candidate, book.bid)  # nunca mejorar por debajo del bid

    def _cancel_and_forget(self, client_order_id: str) -> None:
        self.order_gateway.cancel(client_order_id)
        self._contexts.pop(client_order_id, None)

    def open_order_count(self) -> int:
        return len(self._contexts)

    def has_open_order_for(self, symbol: str) -> bool:
        """
        Usado por run_bot.py para evitar insertar una segunda orden sobre la
        misma base mientras la primera todavia esta en vigilancia (evita
        duplicar exposicion antes de que el fill/cancel de la primera orden
        se refleje).
        """
        return any(ctx.symbol == symbol for ctx in self._contexts.values())
