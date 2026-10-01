"""
invariants.py
==============
Invariantes DUROS de estado del portfolio (Tarea #27 item 4, a pedido
explicito del usuario, sesion 2026-10-01):

    "una estrategia long-only nunca puede quedar neta corta en un
    contrato; ninguna pata corta puede quedar sin su pata larga. Si se
    viola, bloquear la orden y alertar."

Motivados por dos bugs reales ya encontrados y corregidos esta misma
sesion (ver ggal_bot/portfolio/reconciliation.py y
WeeklyAsymmetricStrategy.build_naked_short_wing_exit_signals): esto es la
ULTIMA linea de defensa, pensada para seguir bloqueando aunque un bug
futuro reintroduzca alguno de los dos problemas por otro camino - no
reemplaza esos fixes, los respalda.

Funciones PURAS (sin I/O, sin acceso a self.portfolio directo) - reciben
el Portfolio y devuelven una razon de bloqueo (str) o None. Quien las llama
(run_bot.py) decide que hacer con la razon: loguear ALERTA y no enviar la
orden (ver RiskConfig.enforce_position_invariants, default True).
"""
from __future__ import annotations

from typing import List, Optional

from ggal_bot.portfolio.portfolio import Portfolio


def confirmed_long_quantity(portfolio: Portfolio, symbol: str, strategy_tag: str = "weekly_asymmetric") -> float:
    """
    Duplicado deliberado y minimo de WeeklyAsymmetricStrategy.
    _confirmed_long_quantity (no se importa desde strategy/ para que
    ggal_bot/risk/ no dependa de ggal_bot/strategy/ - evita un ciclo de
    imports y mantiene este modulo utilizable desde cualquier estrategia,
    no solo weekly_asymmetric). Misma semantica exacta: solo cuenta
    exposicion LARGA (quantity > 0) de `symbol` marcada con `strategy_tag`
    (una posicion sin marca cuenta como "weekly_asymmetric").
    """
    return sum(
        p.quantity for p in portfolio.positions
        if p.symbol == symbol and p.quantity > 0 and (p.strategy_tag or "weekly_asymmetric") == strategy_tag
    )


def long_only_net_short_violation(
    portfolio: Portfolio, symbol: str, strategy_tag: str, sell_quantity: float,
) -> Optional[str]:
    """
    INVARIANTE 1: una estrategia long-only (weekly_asymmetric, scalping -
    ninguna de las dos abre posiciones cortas por cuenta propia, solo
    weekly_asymmetric abre la pata corta DELIBERADA de un spread
    financiado, que esta funcion no audita - ver invariante 2) nunca debe
    vender mas contratos de `symbol` que los que tiene confirmados a su
    nombre. Devuelve una razon de bloqueo si `sell_quantity` excede eso,
    None si la venta es segura.

    Esto NUNCA deberia dispararse en el camino normal (run_bot.py::
    _act_on_exit_signal ya reduce lote por lote sin pasarse, ver el fix de
    over-close de Fase 5.3) - es la ultima linea de defensa si un bug
    futuro (o una herramienta manual) intentara vender de mas.
    """
    available = confirmed_long_quantity(portfolio, symbol, strategy_tag)
    if sell_quantity > available + 1e-9:
        return (
            f"{symbol} ({strategy_tag}): se intento vender {sell_quantity:g} contratos pero solo "
            f"hay {available:g} confirmados a nombre de esta estrategia long-only - la venta dejaria "
            "una posicion neta CORTA, prohibido para una estrategia long-only."
        )
    return None


def financed_short_wings_of(portfolio: Portfolio, long_symbol: str, strategy_tag: str):
    """Patas cortas (quantity<0) de `strategy_tag` cuyo financed_by_symbol
    es `long_symbol` - ver Position.financed_by_symbol."""
    return [
        p for p in portfolio.positions
        if p.quantity < 0
        and (p.strategy_tag or "weekly_asymmetric") == strategy_tag
        and p.financed_by_symbol == long_symbol
    ]


def naked_short_wing_violation(
    portfolio: Portfolio, long_symbol: str, strategy_tag: str, reduce_quantity: float,
    wing_is_closable_now,
) -> Optional[str]:
    """
    INVARIANTE 2: ninguna pata corta puede quedar sin su pata larga.
    Si reducir `long_symbol` en `reduce_quantity` dejaria alguna pata corta
    financiada por el sin cobertura, y esa pata corta NO se puede recomprar
    este mismo ciclo (`wing_is_closable_now(wing_symbol) -> bool`, inyectado
    por el llamador - ver run_bot.py: tipicamente "hay cotizacion bid/ask
    operable y no hay una orden ya en vigilancia sobre esa base", igual
    criterio que build_naked_short_wing_exit_signals/
    _act_on_naked_short_wing_exit_signal usan para decidir si pueden actuar
    ESTE ciclo), BLOQUEA la reduccion de la larga - mejor diferirla un
    ciclo que dejar una pata corta real sin ninguna gestion.

    Si la pata corta SI se puede recomprar este mismo ciclo, no bloquea
    nada: build_naked_short_wing_exit_signals + _act_on_naked_short_wing_exit_signal
    (Tarea #27 item 3) la van a cerrar en el MISMO ciclo, antes de que
    quede descubierta de forma observable - bloquear ahi seria mas
    conservador de lo necesario sin ganar nada.
    """
    remaining_long = confirmed_long_quantity(portfolio, long_symbol, strategy_tag) - reduce_quantity
    for wing in financed_short_wings_of(portfolio, long_symbol, strategy_tag):
        wing_qty = abs(wing.quantity)
        if wing_qty > max(remaining_long, 0.0) + 1e-9:
            if not wing_is_closable_now(wing.symbol):
                uncovered = wing_qty - max(remaining_long, 0.0)
                return (
                    f"{long_symbol} ({strategy_tag}): reducir {reduce_quantity:g} contratos dejaria "
                    f"{uncovered:g} contratos de la pata corta {wing.symbol} sin cobertura, y esa pata "
                    "no tiene cotizacion operable ahora mismo para recubrirla en el mismo ciclo - "
                    "se bloquea la reduccion de la larga hasta que se pueda cerrar ambas patas juntas."
                )
    return None


def check_portfolio_invariants(portfolio: Portfolio, strategy_tags: Optional[List[str]] = None) -> List[str]:
    """
    Escaneo PASIVO (no bloquea nada, solo reporta) del estado ACTUAL del
    portfolio contra ambos invariantes - pensado para una alerta periodica
    (ver run_bot.py, corrido una vez por ciclo) que detecte una violacion
    YA EXISTENTE (ej. remanente de un bug anterior a este fix, o una
    ventana muy breve entre dos acciones del mismo ciclo) incluso si
    ningun pre-trade guard la bloqueo. `strategy_tags`: cuales auditar
    (default ["weekly_asymmetric", "scalping"] - las dos long-only
    conocidas hoy).
    """
    tags = strategy_tags if strategy_tags is not None else ["weekly_asymmetric", "scalping"]
    violations: List[str] = []
    for tag in tags:
        # Invariante 1: cualquier Position NEGATIVA de una estrategia
        # long-only que NO tenga financed_by_symbol (es decir, no es una
        # pata corta deliberada de spread) es, por definicion, una
        # violacion ya consumada.
        for pos in portfolio.positions:
            if (pos.strategy_tag or "weekly_asymmetric") != tag:
                continue
            if pos.quantity < -1e-9 and not pos.financed_by_symbol:
                violations.append(
                    f"{pos.symbol} ({tag}): posicion NETA CORTA (qty={pos.quantity:g}) sin "
                    "financed_by_symbol - una estrategia long-only no deberia tener esto."
                )

        # Invariante 2: toda pata corta con financed_by_symbol cuya larga
        # ya no la cubre del todo.
        seen_long_symbols = {p.financed_by_symbol for p in portfolio.positions if p.financed_by_symbol}
        for long_symbol in seen_long_symbols:
            remaining_long = confirmed_long_quantity(portfolio, long_symbol, tag)
            for wing in financed_short_wings_of(portfolio, long_symbol, tag):
                wing_qty = abs(wing.quantity)
                if wing_qty > remaining_long + 1e-9:
                    violations.append(
                        f"{wing.symbol} ({tag}): pata corta de {wing_qty:g} contratos financiada por "
                        f"{long_symbol}, que hoy solo cubre {remaining_long:g} - "
                        f"{wing_qty - remaining_long:g} contratos sin cobertura."
                    )
    return violations
