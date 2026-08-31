"""
delta_hedger.py (strategy)
============================
Motor de delta-hedging automatizado: decide si corresponde rehedgear y con
que instrumento (contado vs. futuro de GGAL, segun spread/profundidad
disponibles en el momento), arma la orden de cobertura y la envia via
MidPriceExecutionEngine.

NOTA DE UBICACION: la logica de decision de delta-hedging vivia antes en
execution/delta_hedger.py; se traslado a la capa de estrategia porque
"cuando y cuanto rehedgear" es una decision de estrategia (usa las griegas
del portafolio), mientras que execution/ solo se ocupa de "como" insertar
una orden dada una decision ya tomada (ver execution/mid_price_exec.py).
execution/delta_hedger.py se dejo como alias de compatibilidad hacia este
modulo (ver ese archivo).

Las ordenes de cobertura se envian de forma AGRESIVA (cruzando el spread)
por decision de diseño: el riesgo de NO cubrir el delta a tiempo (quedar
expuesto direccionalmente) pesa mas que el spread que se paga por
garantizar la ejecucion. Ver docs de diseño, secciones 1.2 y 2.4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ggal_bot.data.option_chain import OrderBookSnapshot
from ggal_bot.execution.mid_price_exec import MidPriceExecutionEngine
from ggal_bot.execution.order_gateway import OrderSide, OrderState

logger = logging.getLogger("ggal_bot.strategy.delta_hedger")


@dataclass
class HedgeInstruction:
    instrument: str          # "GGAL_CONTADO" o "GGAL_FUTURO"
    quantity: float           # + comprar, - vender


class DeltaHedgingEngine:
    def __init__(self, delta_band: float = 150.0):
        self.delta_band = delta_band

    # -- Decision: cuando y cuanto rehedgear ---------------------------------

    def needs_hedge(self, portfolio_delta: float) -> bool:
        """
        Umbral de delta-neutralidad (ver config.RiskConfig.delta_band): un
        delta remanente dentro de la banda se considera ruido, no riesgo
        real; solo se actua cuando se excede, para no sobre-operar y pagar
        spread/comisiones por variaciones irrelevantes.
        """
        return abs(portfolio_delta) > self.delta_band

    def build_hedge(
        self,
        portfolio_delta: float,
        contado_book: OrderBookSnapshot,
        futuro_book: Optional[OrderBookSnapshot],
        min_size: float = 50.0,
        max_spread_relative: float = 0.01,
    ) -> Optional[HedgeInstruction]:
        if not self.needs_hedge(portfolio_delta):
            return None
        # Se neutraliza el EXCEDENTE por sobre la banda, no se lleva el
        # delta a cero: cubrir hasta cero implicaria rehedgear por cada
        # variacion minima, generando costos de transaccion innecesarios.
        excess = abs(portfolio_delta) - self.delta_band
        qty_needed = -1 * excess if portfolio_delta > 0 else excess

        futuro_ok = (
            futuro_book is not None
            and futuro_book.is_tradeable(max_spread_relative, min_size)
        )
        contado_ok = contado_book.is_tradeable(max_spread_relative, min_size)

        if futuro_ok and (not contado_ok or futuro_book.spread_relative < contado_book.spread_relative):
            return HedgeInstruction(instrument="GGAL_FUTURO", quantity=qty_needed)
        if contado_ok:
            return HedgeInstruction(instrument="GGAL_CONTADO", quantity=qty_needed)
        return None  # ninguna ruta de cobertura es operable: escalar alerta, no operar a ciegas

    # -- Ejecucion: arma y envia la orden de cobertura -----------------------

    def execute_hedge(
        self,
        portfolio_delta: float,
        contado_book: OrderBookSnapshot,
        futuro_book: Optional[OrderBookSnapshot],
        mid_price_engine: MidPriceExecutionEngine,
        min_size: float = 50.0,
        max_spread_relative: float = 0.01,
    ) -> Optional[OrderState]:
        """
        Orquesta build_hedge() + el envio real de la orden. Devuelve el
        OrderState si se genero y envio una orden, o None si no hacia falta
        rehedgear o si ninguna ruta de cobertura era operable (en ese
        segundo caso, RiskManager/run_bot.py deben escalar una alerta: el
        portafolio sigue con delta fuera de banda sin forma de corregirlo
        de inmediato).
        """
        instruction = self.build_hedge(
            portfolio_delta, contado_book, futuro_book, min_size, max_spread_relative,
        )
        if instruction is None:
            return None

        book = futuro_book if instruction.instrument == "GGAL_FUTURO" and futuro_book else contado_book
        side = OrderSide.BUY if instruction.quantity > 0 else OrderSide.SELL
        quantity = abs(instruction.quantity)

        logger.info(
            "Ejecutando hedge de delta: %s %.0f en %s (delta portafolio=%.2f, banda=%.2f)",
            side.value, quantity, instruction.instrument, portfolio_delta, self.delta_band,
        )
        return mid_price_engine.submit(
            symbol=book.symbol, book=book, side=side, quantity=quantity,
            spot_reference=contado_book.mid, aggressive=True,
        )
