"""
reconciliation.py
==================
Reconciliacion de estado al arranque (Fase 5.3). Ver
AUDITORIA_FASE5.2B_FORENSIC_REPLAY.md SS13 y el resultado de
test_fase53_guard2_replay.py (Fase 5.3, "ROOT CAUSE RESOLVED"):

    Guarda 2 (_position_quantity(symbol) != 0, run_bot.py:
    _act_on_entry_signal) funciona correctamente en un proceso continuo -
    probado con un replay ejecutable contra el codigo real. La unica
    explicacion que sobrevive para la contradiccion observada en produccion
    (3 BUY consecutivos sobre GFGC7000OC sin ninguna venta entre medio,
    todos ejecutados) es que el PROCESO se reinicio entre fills, y
    GgalOptionsBot.__init__ crea `self.portfolio = Portfolio()` SIEMPRE
    vacio - no existe, en ningun lado del codigo (confirmado por grep
    exhaustivo de "load_state|restore|resume"), ninguna logica que
    reconstruya el portfolio desde el historial de fills al arrancar.

Este modulo cierra ESE gap especifico: reconstruye la posicion NETA
abierta por simbolo desde logs/shadow_trades.csv (el mismo archivo, con el
mismo algoritmo FIFO ya verificado byte a byte contra produccion en Fase
5/5.1 - dashboard/pnl_engine.py::match_trades_fifo, NO una reimplementacion
nueva) y expone `reconstruct_positions_from_shadow_log()` para que
run_bot.py::connect_and_subscribe() lo llame ANTES de que arranque el loop
principal, poblando self.portfolio antes de que Guarda 2 evalue ninguna
señal.

CONSOLIDACION (agregado el 2026-09-07, tras el primer trip real del kill
switch en produccion - ver AUDITORIA_FASE5.2_LIFECYCLE_ROOT_CAUSE.md):
match_trades_fifo() devuelve un OpenLot por CADA fill BUY sin cerrar
todavia, no uno por simbolo - eso es correcto para el motor de PnL (cada
lote conserva su propio precio/tiempo de entrada para el calculo FIFO de
cierres futuros), pero viola el invariante de diseño del resto del sistema
("como maximo una Position por simbolo+estrategia", el mismo que
KillSwitch.evaluate() audita via max_positions_per_symbol_strategy). La
version original de esta funcion creaba un Position por OpenLot crudo: en
produccion, 5 bases con fragmentacion HISTORICA real (fills BUY repetidos
sobre el mismo simbolo antes de que el fix de Fase 5.3 les pusiera
contract_key/journal) se reconstruian como 2 a 6 Position "simultaneas"
sobre la misma base - el kill switch disparo INMEDIATAMENTE al arrancar,
correctamente detectando esa fragmentacion (el chequeo funciono como
estaba pensado), pero el estado reconstruido no reflejaba el invariante
que el resto del bot asume. Fix: se consolidan los OpenLot del mismo
simbolo+signo en una sola fila via dashboard/pnl_engine.py::
aggregate_open_positions() (ya existente y testeado - no reimplementado
aca) ANTES de construir los Position - precio de entrada promedio
ponderado por cantidad, entry_time el mas antiguo del grupo. Esto no
descarta informacion: la cantidad NETA y el costo promedio ponderado son
exactamente lo que Guarda 2 y el calculo de P&L no realizado necesitan; lo
que se pierde (el detalle por-lote de cuando entro cada fraccion) no lo
usa ningun consumidor de self.portfolio hoy.

ALCANCE EXPLICITO - que NO resuelve este modulo:
  1. Modo LIVE (self.shadow_mode=False, ordenes reales via pyRofex): la
     reconciliacion "de verdad" en ese modo deberia ser contra el estado de
     cuenta real del broker (OrderGateway.get_account_positions()), no
     contra un CSV local - eso es un problema DISTINTO (y mas dificil,
     porque implica mapear posiciones del broker a Position con toda su
     metadata de riesgo) que queda fuera de esta fase. Este modulo solo
     actua si self.shadow_mode es True.
  2. strategy_tag: logs/shadow_trades.csv NO persiste este campo (ver
     ShadowAuditLogger._HEADER en execution/order_gateway.py) - ver el
     WARNING explicito que emite esta funcion cuando SETTINGS.scalping.
     enabled=True, y la nota en Position.strategy_tag sobre por que
     defaultear a None (weekly_asymmetric) es seguro HOY (scalping
     deshabilitado por defecto) pero podria no serlo si scalping opera el
     mismo simbolo que weekly_asymmetric.
  3. Griegas (`Position.greeks_per_unit`): se completan con la cotizacion
     VIGENTE de self.option_chain al momento de reconciliar (si esa base
     sigue en el universo activo) - no con las griegas del momento del
     fill original (esas no se loguean en ningun lado, ver limitacion ya
     documentada de que greeks_per_unit nunca se refresca post-creacion en
     el codigo existente). Si la base ya no esta en el universo vigente
     (vencio, rodo fuera de rango), greeks_per_unit queda en None
     (tratada por Position.contribution() como delta=1 por unidad) y se
     reporta en `warnings` - NUNCA se fabrica un valor de griega.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from ggal_bot import paths
from ggal_bot.config import SETTINGS
from ggal_bot.portfolio.portfolio import Position

logger = logging.getLogger("ggal_bot.portfolio.reconciliation")


class ReconciliationUnavailable(Exception):
    """
    Se levanta cuando la reconciliacion NO se pudo ejecutar por falta de
    una dependencia opcional (pandas/numpy, ver dashboard/pnl_engine.py) -
    NUNCA para representar un problema de datos (eso va en `warnings`, ver
    reconstruct_positions_from_shadow_log()). El llamador
    (run_bot.py::connect_and_subscribe) debe atrapar esta excepcion
    puntual y seguir con un Portfolio() vacio - comportamiento identico al
    de antes de esta fase - en vez de abortar el arranque del bot.

    MOTIVO de la dependencia opcional: dashboard.pnl_engine importa
    pandas/numpy, que estan en requirements-dashboard.txt pero
    DELIBERADAMENTE NO en requirements.txt (ver el comentario en ese
    archivo: para que "python run_bot.py" standalone/local no dependa de
    ellos). En Northflank, el Dockerfile instala ambos requirements juntos
    (bot + dashboard en el mismo contenedor), asi que en produccion esto
    SIEMPRE deberia estar disponible - pero un entorno local sin `pip
    install -r requirements-dashboard.txt` no lo tendria, y el bot debe
    poder arrancar igual (sin reconciliacion, con un warning) en vez de
    crashear por una dependencia que antes de esta fase ni siquiera hacia
    falta.
    """


def reconstruct_positions_from_shadow_log(
    csv_path=None,
    option_multiplier: Optional[float] = None,
    option_chain=None,
) -> Tuple[List[Position], List[str]]:
    """
    Devuelve (positions, warnings).

    `positions`: un Position por cada simbolo (+signo de direccion) que
    queda con cantidad neta ABIERTA tras procesar TODO el historial de
    logs/shadow_trades.csv (o `csv_path`) con el mismo FIFO que ya usa el
    dashboard - ver dashboard/pnl_engine.py::match_trades_fifo, NO
    reimplementado aca a proposito (ese algoritmo ya fue verificado, fila
    por fila, contra produccion en Fase 5/5.1). Los lotes abiertos del
    mismo simbolo+signo que devuelve match_trades_fifo se CONSOLIDAN en un
    unico Position via dashboard/pnl_engine.py::aggregate_open_positions()
    (precio de entrada promedio ponderado por cantidad, entry_time el mas
    antiguo) - ver nota "CONSOLIDACION" en el docstring del modulo.

    `warnings`: texto humano no fatal - cada limitacion real de esta
    reconstruccion (ver docstring del modulo) se reporta aca. Nunca se
    fabrica un dato para evitar generar un warning.

    Levanta ReconciliationUnavailable si dashboard.pnl_engine no se puede
    importar (pandas/numpy ausentes en este entorno).
    """
    try:
        from dashboard.pnl_engine import (
            _is_underlying_symbol,
            aggregate_open_positions,
            load_fills,
            match_trades_fifo,
        )
    except ImportError as exc:
        raise ReconciliationUnavailable(
            "No se pudo importar dashboard.pnl_engine para reconciliar el portfolio al "
            "arranque (falta pandas/numpy en este entorno - ver requirements-dashboard.txt "
            f"y su comentario sobre por que run_bot.py no las requiere por defecto): {exc}"
        ) from exc

    warnings: List[str] = []
    # Lectura PEREZOSA de paths.SHADOW_TRADES_LOG (nunca "from ... import
    # SHADOW_TRADES_LOG" arriba, que capturaria el valor al importar este
    # modulo) - mismo criterio que execution/order_gateway.py::
    # ShadowAuditLogger, documentado en
    # ggal_bot/validation/_shadow_audit_isolation.py: permite que un test
    # (o el aislamiento global de tests) redirija paths.SHADOW_TRADES_LOG
    # DESPUES de que este modulo ya fue importado.
    path = csv_path if csv_path is not None else paths.SHADOW_TRADES_LOG
    mult = option_multiplier if option_multiplier is not None else SETTINGS.instruments.option_multiplier

    fills = load_fills(path)
    if fills.empty:
        return [], warnings

    _closed, open_lots = match_trades_fifo(fills, option_multiplier=mult)

    if SETTINGS.scalping.enabled:
        warnings.append(
            "GGAL_BOT_ENABLE_SCALPING=true: logs/shadow_trades.csv no persiste strategy_tag "
            "(no existe ese campo en el schema de ShadowAuditLogger), asi que esta "
            "reconciliacion NO puede distinguir lotes de scalping vs weekly_asymmetric sobre "
            "el mismo simbolo. Todos los lotes reconstruidos se etiquetan weekly_asymmetric "
            "(strategy_tag=None) por convencion - puede ser incorrecto si scalping opero ese "
            "simbolo. A partir de este deploy, ggal_bot/portfolio/event_journal.py SI "
            "persiste strategy_tag para eventos nuevos (no ayuda a reconciliar historial "
            "previo a este cambio)."
        )

    # Consolida lotes abiertos del mismo simbolo+signo en una sola fila
    # (precio de entrada promedio ponderado por cantidad, entry_time el mas
    # antiguo) - funcion ya existente y testeada, reusada tal cual (ver nota
    # "CONSOLIDACION" en el docstring del modulo). Sin esto, cada BUY sin
    # cerrar todavia sobre el mismo simbolo se convertia en una Position
    # "simultanea" distinta, violando el invariante de una sola Position
    # por simbolo+estrategia que el resto del bot (incluido KillSwitch.
    # evaluate) asume.
    aggregated = aggregate_open_positions(open_lots)

    positions: List[Position] = []
    for row in aggregated.itertuples(index=False):
        symbol = row.symbol
        is_underlying = _is_underlying_symbol(symbol)
        greeks_per_unit = None
        expiry = None
        data_unavailable = []
        if not is_underlying:
            quote = option_chain.get(symbol) if option_chain is not None else None
            if quote is not None and quote.greeks is not None:
                greeks_per_unit = quote.greeks
                expiry = quote.expiry
            else:
                data_unavailable.append("greeks_per_unit")
                warnings.append(
                    f"{symbol}: se reconstruyo la posicion (qty={row.quantity}) pero no "
                    "hay cotizacion vigente en el option_chain actual para completar sus "
                    "griegas (probable base fuera del universo activo: vencida o rolleada) - "
                    "greeks_per_unit queda en None (Position.contribution() la trata como "
                    "delta=1 por unidad hasta que una entrada nueva la reemplace o se cierre)."
                )

        entry_time = row.entry_time
        if hasattr(entry_time, "to_pydatetime"):
            entry_time = entry_time.to_pydatetime()

        positions.append(Position(
            symbol=symbol,
            quantity=row.quantity,
            multiplier=1.0 if is_underlying else mult,
            greeks_per_unit=greeks_per_unit,
            expiry=expiry,
            entry_price=row.avg_entry_price,
            entry_time=entry_time,
            strategy_tag=None,
        ))
        if data_unavailable:
            logger.warning(
                "Reconciliacion de arranque: %s reconstruido con campos incompletos (%s).",
                symbol, ", ".join(data_unavailable),
            )

    return positions, warnings
