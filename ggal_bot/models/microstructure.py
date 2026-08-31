"""
microstructure.py
====================
Señales de microestructura de la punta (bid/ask + tamaños) usadas como
CONFIRMACION DE CALIDAD DE EJECUCION antes de tomar una entrada — no como
predictor direccional de corto plazo. Ver la nota de alcance abajo, es
importante para entender por que este modulo es deliberadamente chico.

Order Book Imbalance (OBI):

    OBI = (bid_size - ask_size) / (bid_size + ask_size)

Rango [-1, 1]: positivo = mas tamaño parado en la punta compradora que en
la vendedora, negativo = lo contrario.

NOTA DE ALCANCE (por que esto NO es un predictor de alpha aca): en un libro
tan delgado como el de opciones de GGAL en BYMA (pocos participantes, sin
un market maker continuo dedicado en cada base) y con un ciclo de recalculo
de ~2-4s (no tick-by-tick, no colas de order flow), el OBI de un instante
dado es demasiado ruidoso para usarse como señal de alpha direccional de
corto plazo — eso tendria sentido en un contexto de HFT con libro profundo
y latencia de microsegundos, que no es el caso aca. Se usa entonces como un
FILTRO DE CALIDAD DE EJECUCION: evita levantar la oferta (comprar) justo
cuando el libro muestra un desbalance EXTREMO hacia el lado vendedor, que en
un mercado delgado suele reflejar una punta aislada/iliquida (un unico
participante grande del lado vendedor, quizas por un motivo ajeno a la
opcion en si) mas que informacion genuina de precio. Es un guardrail, no
una prediccion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ggal_bot.data.option_chain import OrderBookSnapshot


def order_book_imbalance(book: "OrderBookSnapshot") -> float:
    """
    (bid_size - ask_size) / (bid_size + ask_size). Devuelve 0.0 (neutral) si
    la fuente de datos no informa tamaños de punta (bid_size=ask_size=0),
    para no penalizar a una fuente que simplemente no reporta profundidad.
    """
    total = book.bid_size + book.ask_size
    if total <= 0:
        return 0.0
    return (book.bid_size - book.ask_size) / total


def passes_obi_filter(book: "OrderBookSnapshot", min_obi: float) -> bool:
    """
    True si el desbalance del libro NO esta por debajo del piso configurado
    (es decir, si el libro no muestra un desbalance extremo hacia el lado
    vendedor). Un min_obi mas negativo es mas permisivo (bloquea menos
    casos); min_obi=-1.0 nunca bloquea nada (rango completo de OBI).
    """
    return order_book_imbalance(book) >= min_obi
