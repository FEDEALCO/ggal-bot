"""
market_making.py
=================
Calcula el precio de insercion de una orden segun la liquidez disponible:
en bases liquidas cruza el spread (agresivo); en bases ilíquidas cotiza al
mid-price redondeado al tick, capturando spread en vez de pagarlo. Ver
docs de diseño, seccion 2.4.
"""

from __future__ import annotations

from ggal_bot.data.option_chain import OrderBookSnapshot


class MarketMakingEngine:
    def __init__(self, tick_size: float = 0.01, liquid_spread_relative_threshold: float = 0.02):
        self.tick_size = tick_size
        self.liquid_spread_relative_threshold = liquid_spread_relative_threshold

    def round_to_tick(self, price: float) -> float:
        return round(price / self.tick_size) * self.tick_size

    def decide_price(self, book: OrderBookSnapshot, side: str) -> float:
        """side: 'buy' o 'sell'."""
        if book.spread_relative <= self.liquid_spread_relative_threshold:
            # mercado ajustado: agredir al mejor precio disponible
            return book.ask if side == "buy" else book.bid
        # mercado ilíquido: cotizar en el medio para capturar spread
        return self.round_to_tick(book.mid)
