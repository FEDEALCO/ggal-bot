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

ACTUALIZACION 2026-10-01 (a pedido explicito del usuario, tras la Tarea #27
"posiciones fantasma"): `reconstruct_positions_from_shadow_log()` (arriba)
se mantiene SIN CAMBIOS por compatibilidad hacia atras (dashboard/backtest
la mencionan en comentarios pero NINGUN codigo productivo la llama salvo
`run_bot.py`, y varios tests existentes la ejercitan directamente) - pero
YA NO es lo que usa el arranque del bot (ver `_reconcile_portfolio_on_startup`
en run_bot.py). Dos bugs reales, verificados contra el CSV de produccion
(17a8486f-position_events_1.csv, hasta 2026-10-01 17:08 UTC) motivaron el
reemplazo:

  BUG A (contaminacion entre estrategias): esta funcion hace FIFO sobre
  logs/shadow_trades.csv, que NO tiene columna `strategy_tag` - cae al
  clasificador viejo `classify_strategy(symbol)` (SIEMPRE "vol_arbitrage"
  para cualquier opcion), mezclando en un solo Position los fills de
  weekly_asymmetric Y scalping sobre el mismo simbolo. VERIFICADO: en
  GFGC6600OC, weekly_asymmetric real neto=0 (totalmente cerrada) y scalping
  real neto=+8 (lote vigente) se mezclaban en una sola Position qty=1.0
  etiquetada weekly_asymmetric (ninguna de las dos cifras reales).

  BUG B (position_id se reinventa en cada restart): `Position.position_id`
  usa `field(default_factory=...)` - cada vez que este modulo reconstruye
  una Position (en cada restart del proceso, ya que self.portfolio nunca
  persiste) genera un UUID NUEVO, sin relacion con el position_id original
  de la ENTRY que abrio esa posicion. VERIFICADO contra produccion: CASI
  CADA transicion ENTRY->PARTIAL_EXIT->CLOSE de weekly_asymmetric tiene un
  position_id DISTINTO en cada evento (ej. GFGC6600OC 2026-09-08/09/11:
  ENTRY=23cc49b7d042, PARTIAL_EXIT=ac1912b8e4ac, CLOSE=99a2ff46c818- tres
  IDs para la MISMA posicion economica), consistente con un restart del
  proceso entre cada evento. Esto rompe cualquier trazabilidad ENTRY<->CLOSE
  basada en position_id para posiciones que sobreviven un restart.

  FIX (`reconstruct_positions_from_event_journal()`, mas abajo): reconstruye
  desde logs/position_events.csv (el Event Journal) en vez de
  shadow_trades.csv. El journal YA tiene `strategy_tag` correcto por evento
  (puesto por el propio codigo de entrada/salida al momento real del fill,
  nunca inferido) - agrupar por (symbol, strategy_tag) tal como el journal
  los grabo elimina el BUG A de raiz, sin reimplementar ninguna heuristica
  de clasificacion. Para el BUG B: en vez de fabricar un position_id nuevo,
  esta funcion reusa el position_id del evento MAS RECIENTE de cada grupo
  todavia abierto - de ahora en mas (ver docstring de la funcion), un
  restart ya NO fabrica una identidad nueva, porque la reconciliacion misma
  pasa a ser la fuente que preserva la identidad existente en el journal.

  LIMITACION EXPLICITA (no resuelta, no se puede resolver solo con datos):
  el journal registra el strategy_tag que el CODIGO EN EJECUCION creia
  correcto en ese momento - si ese codigo ya estaba confundido (ej. una
  Position contaminada por el BUG A en un restart ANTERIOR a este fix, cuyo
  strategy_tag heredado quedo mal puesto), las reducciones posteriores de
  esa misma Position quedan grabadas en el journal bajo la etiqueta
  incorrecta tambien, y ningun analisis posterior del journal solo puede
  distinguir esto con certeza. VERIFICADO: GFGC6600OC weekly_asymmetric
  tiene 3 eventos PARTIAL_EXIT/CLOSE el 2026-09-30 (-4,-2,-1) que esta
  funcion NO puede asociar a ningun lote abierto segun el journal (el unico
  lote weekly_asymmetric de esa base, ENTRY 8073aff38d40 de 16 contratos,
  ya aparece CERRADO por completo un dia antes, CLOSE afed1e5f148c -16 a
  382.0 el 2026-09-29 13:28 UTC/10:28 ART - fuera de horario de rueda,
  antes del fix de gate de horario) - se reportan como `orphan_reductions`
  en el resultado, NUNCA se fabrica a cual lote "en realidad" pertenecian.
  Este es exactamente el tipo de contaminacion historica irreversible que
  `GGAL_BOT_SHADOW_RESET_ON_START` (ver ops/shadow_reset.py) esta pensado
  para resolver de una vez, en vez de intentar reconstruirla.

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
     sigue en el universo activo Y tiene punta de dos lados vigente para
     calcular IV) - no con las griegas del momento del fill original (esas
     no se loguean en ningun lado, ver limitacion ya documentada de que
     greeks_per_unit nunca se refresca post-creacion en el codigo
     existente). Si la base ya no esta en el universo vigente (vencio,
     rodo fuera de rango) O esta en el universo pero sin punta vigente
     ahora mismo (iliquida), greeks_per_unit queda en None (tratada por
     Position.contribution() como delta=1 por unidad) y se reporta en
     `warnings` - NUNCA se fabrica un valor de griega. `Position.expiry`
     es INDEPENDIENTE de esto (fix 2026-09-08, ver el bloque `if not
     is_underlying` de abajo): es un dato estatico de la definicion del
     instrumento, se completa apenas la base sigue en el universo activo,
     aunque sus griegas no se hayan podido calcular todavia - sin este
     fix, build_exit_signals() saltaba (por falta de expiry) cualquier
     posicion reconstruida que no tuviera cotizacion de dos lados en ese
     instante, aunque estuviera perfectamente identificada en el universo.
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
            # BUG REAL VERIFICADO (2026-09-08, ver el log de produccion tras
            # activar el modo aditivo weekly_asymmetric+scalping: las 6
            # bases huerfanas seguian sin evaluarse en SL/TP/horizonte
            # porque esta funcion las reconstruia con expiry=None): antes,
            # `expiry` solo se completaba DENTRO del mismo `if quote.greeks
            # is not None`, atado a la disponibilidad de griegas - pero
            # OptionQuote.expiry es un campo ESTATICO de la definicion del
            # instrumento (ver data/option_chain.py: se fija al bootstrapear
            # el universo, nunca depende de si hay o no punta bid/ask
            # vigente para calcular IV/griegas). Una base bien adentro del
            # universo activo pero momentaneamente sin cotizacion de dos
            # puntas (iliquida, lejos del spot) quedaba con expiry=None
            # igual que si estuviera realmente vencida/fuera del universo -
            # y build_exit_signals() salta cualquier posicion con
            # `expiry is None` ("no se puede evaluar Stop Loss/Take
            # Profit"), asi que esa base quedaba SIN gestion de riesgo
            # indefinidamente, incluso bajo el modo aditivo que en teoria ya
            # la cubre por strategy_tag. Fix: `expiry` se completa apenas
            # `quote` existe (dato real, no fabricado - viene de la
            # definicion del instrumento); `greeks_per_unit`/el warning
            # siguen dependiendo, como antes, de que `quote.greeks` este
            # calculado.
            quote = option_chain.get(symbol) if option_chain is not None else None
            if quote is not None:
                expiry = quote.expiry
            if quote is not None and quote.greeks is not None:
                greeks_per_unit = quote.greeks
            else:
                data_unavailable.append("greeks_per_unit")
                if quote is None:
                    warnings.append(
                        f"{symbol}: se reconstruyo la posicion (qty={row.quantity}) pero no "
                        "existe en el option_chain actual (probable base fuera del universo "
                        "activo: vencida o rolleada) - ni expiry ni greeks_per_unit se pueden "
                        "completar; build_exit_signals() va a saltear esta posicion por "
                        "completo hasta que se cierre a mano (ver ggal_bot/ops/manual_close.py)."
                    )
                else:
                    warnings.append(
                        f"{symbol}: se reconstruyo la posicion (qty={row.quantity}) - esta en "
                        "el universo activo (expiry recuperado de la definicion del "
                        "instrumento) pero sin punta de dos lados vigente para calcular IV/"
                        "griegas ahora mismo (probable base iliquida/lejos del spot) - "
                        "greeks_per_unit queda en None (Position.contribution() la trata como "
                        "delta=1 por unidad hasta que una entrada nueva la reemplace o se "
                        "cierre); el horizonte/guardia de fin de semana SI se evaluan igual "
                        "(no dependen de griegas), Stop Loss/Take Profit siguen sin poder "
                        "evaluarse hasta que haya un precio vigente."
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


class OrphanReduction:
    """
    Una reduccion (REDUCE/PARTIAL_EXIT/CLOSE) del Event Journal que
    `reconstruct_positions_from_event_journal()` NO pudo asociar a ningun
    lote abierto de su mismo (symbol, strategy_tag) - ver "LIMITACION
    EXPLICITA" en el docstring del modulo. Nunca se descarta en silencio:
    se guarda aca con el detalle completo para que quede en `warnings` y
    quien lea el log pueda decidir a mano.
    """

    __slots__ = ("timestamp_utc", "symbol", "strategy_tag", "event_type", "position_id", "reason", "unmatched_quantity")

    def __init__(self, timestamp_utc, symbol, strategy_tag, event_type, position_id, reason, unmatched_quantity):
        self.timestamp_utc = timestamp_utc
        self.symbol = symbol
        self.strategy_tag = strategy_tag
        self.event_type = event_type
        self.position_id = position_id
        self.reason = reason
        self.unmatched_quantity = unmatched_quantity

    def __repr__(self) -> str:  # pragma: no cover - solo para logs/debug
        return (
            f"OrphanReduction({self.timestamp_utc}, {self.symbol}, {self.strategy_tag}, "
            f"{self.event_type}, position_id={self.position_id}, reason={self.reason!r}, "
            f"unmatched_quantity={self.unmatched_quantity:g})"
        )


def _fifo_remaining_lot(events: List[dict]) -> Tuple[Optional[dict], List[dict]]:
    """
    FIFO generico sobre una lista de eventos de journal YA FILTRADA a UN
    solo (symbol, strategy_tag), ordenada cronologicamente. A diferencia de
    dashboard/pnl_engine.py::match_trades_fifo (que opera sobre fills
    crudos de shadow_trades.csv, side="buy"/"sell"), esto opera directo
    sobre `quantity_delta` con signo - no asume que un grupo es "largo-only":
    el SIGNO del primer evento que abre el lote determina la direccion (una
    pata corta de spread_completion abre con quantity_delta negativo, y se
    reduce con deltas positivos - ver _act_on_spread_completion_signal).

    Regla: ENTRY/ADD siempre ABRE/AGREGA un lote nuevo (cualquiera sea su
    signo - una pata corta de spread_completion abre con quantity_delta
    negativo, ver mas arriba); REDUCE/PARTIAL_EXIT/CLOSE siempre CONSUME la
    cola, mas antiguo primero (FIFO), sea cual sea su propio signo. Si un
    evento de cierre pide consumir mas de lo que queda en cola (o la cola
    esta vacia), el remanente sin poder asociar se devuelve en `orphans` -
    NUNCA se interpreta una reduccion sin lote previo como si abriera un
    lote nuevo (esa confusion fue, de hecho, el primer intento de esta
    funcion y se descarto: ver test_orphan_reduction_with_no_prior_entry_at_all_is_reported).

    Devuelve (lote_remanente_o_None, orphans). `lote_remanente`:
    {"quantity", "price" (promedio ponderado por |qty| de los lotes
    sobrevivientes), "entry_time" (el mas antiguo de los sobrevivientes),
    "position_id" (el del evento MAS RECIENTE del grupo completo - ver
    "BUG B" en el docstring del modulo: de ahora en mas esta es la
    identidad que se preserva en restarts sucesivos, en vez de fabricar
    una nueva cada vez)}.
    """
    queue: List[dict] = []
    orphans: List[dict] = []
    last_event: Optional[dict] = None

    for ev in events:
        delta = ev["quantity_delta"]
        if delta is None or (isinstance(delta, float) and delta != delta):  # NaN
            continue
        last_event = ev
        if delta == 0:
            continue

        if ev["event_type"] in ("ENTRY", "ADD"):
            queue.append({
                "quantity": delta, "price": ev["price"],
                "entry_time": ev["timestamp_utc"], "position_id": ev["position_id"],
            })
            continue

        # REDUCE/PARTIAL_EXIT/CLOSE: SIEMPRE consume la cola (nunca abre un
        # lote nuevo, sea cual sea su propio signo) - si no hay nada que
        # consumir, es un huerfano (ver docstring de arriba).
        to_consume = abs(delta)
        while to_consume > 1e-9 and queue:
            lot = queue[0]
            lot_abs = abs(lot["quantity"])
            take = min(lot_abs, to_consume)
            lot["quantity"] = lot["quantity"] - take if lot["quantity"] > 0 else lot["quantity"] + take
            to_consume -= take
            if abs(lot["quantity"]) < 1e-9:
                queue.pop(0)
        if to_consume > 1e-9:
            orphans.append({**ev, "unmatched_quantity": to_consume})

    if not queue:
        return None, orphans

    total_qty = sum(l["quantity"] for l in queue)
    weight = sum(abs(l["quantity"]) for l in queue)
    avg_price = (sum(l["price"] * abs(l["quantity"]) for l in queue) / weight) if weight else 0.0
    oldest = min(queue, key=lambda l: l["entry_time"])
    remaining = {
        "quantity": total_qty, "price": avg_price, "entry_time": oldest["entry_time"],
        # Identidad: el position_id del evento MAS RECIENTE de TODO el grupo
        # (no solo de los lotes sobrevivientes) - es la ultima identidad que
        # el journal registro para este (symbol, strategy_tag), la mas
        # razonable para seguir usando de ahora en mas (ver "BUG B").
        "position_id": last_event["position_id"] if last_event is not None else oldest["position_id"],
    }
    return remaining, orphans


def reconstruct_positions_from_event_journal(
    csv_path=None,
    option_multiplier: Optional[float] = None,
    option_chain=None,
) -> Tuple[List[Position], List[str]]:
    """
    Reemplazo de `reconstruct_positions_from_shadow_log()` para el arranque
    del bot (ver run_bot.py::_reconcile_portfolio_on_startup) - reconstruye
    desde logs/position_events.csv (el Event Journal, ver
    ggal_bot/portfolio/event_journal.py) en vez de logs/shadow_trades.csv.
    Ver "ACTUALIZACION 2026-10-01" en el docstring del modulo para el
    detalle completo de los dos bugs reales que motivan este reemplazo
    (contaminacion entre estrategias / position_id que se reinventa en cada
    restart) y la limitacion explicita que sigue sin poder resolverse solo
    con datos (reducciones "huerfanas" por contaminacion HISTORICA anterior
    a este fix).

    Devuelve (positions, warnings) - misma forma que la funcion que
    reemplaza. `warnings` incluye, ademas de los casos ya documentados ahi
    (griegas/expiry no disponibles), una linea por cada OrphanReduction
    encontrada (ver _fifo_remaining_lot).

    Agrupa por (symbol, strategy_tag) usando el strategy_tag QUE EL JOURNAL
    YA TIENE por evento (una fila sin strategy_tag, de un evento anterior a
    que este campo existiera, cuenta como "weekly_asymmetric" - mismo
    criterio que Portfolio.greeks_for_strategy_tag) - nunca se re-infiere
    via el simbolo (eso es, exactamente, el BUG A que esto reemplaza).

    Levanta ReconciliationUnavailable si dashboard.pnl_engine no se puede
    importar (misma dependencia opcional que la funcion que reemplaza).
    """
    try:
        from dashboard.pnl_engine import _is_underlying_symbol, load_position_events
    except ImportError as exc:
        raise ReconciliationUnavailable(
            "No se pudo importar dashboard.pnl_engine para reconciliar el portfolio al "
            "arranque desde el Event Journal (falta pandas/numpy en este entorno - ver "
            f"requirements-dashboard.txt): {exc}"
        ) from exc

    warnings: List[str] = []
    path = csv_path if csv_path is not None else paths.POSITION_EVENTS_LOG
    mult = option_multiplier if option_multiplier is not None else SETTINGS.instruments.option_multiplier

    df = load_position_events(path)
    if df.empty:
        return [], warnings

    df = df[df["event_type"].isin(("ENTRY", "ADD", "REDUCE", "PARTIAL_EXIT", "CLOSE"))].copy()
    if df.empty:
        return [], warnings
    df["quantity_delta"] = _to_numeric(df["quantity_delta"])
    df["price"] = _to_numeric(df["price"])
    df = df.dropna(subset=["timestamp_utc", "quantity_delta", "symbol"])
    df["strategy_tag"] = df["strategy_tag"].fillna("").replace("", "weekly_asymmetric")
    df = df.sort_values("timestamp_utc")

    positions: List[Position] = []
    for (symbol, strategy_tag), group in df.groupby(["symbol", "strategy_tag"], sort=False):
        events = group.to_dict("records")
        remaining, orphans = _fifo_remaining_lot(events)

        for orphan in orphans:
            warnings.append(
                f"{symbol} ({strategy_tag}): evento {orphan['event_type']} "
                f"(position_id={orphan['position_id']}, reason={orphan['reason']!r}, "
                f"{orphan['timestamp_utc']}) no se pudo asociar a ningun lote abierto segun "
                f"el Event Journal (cantidad sin match: {orphan['unmatched_quantity']:g}) - "
                "posible contaminacion historica de un restart anterior a este fix (ver "
                "'ACTUALIZACION 2026-10-01' en reconciliation.py), o posicion legacy anterior "
                "al deploy del journal (2026-09-07 17:05 UTC). No se fabrica una resolucion; "
                "revisar a mano (ggal_bot/ops/manual_close.py) si corresponde."
            )

        if remaining is None or abs(remaining["quantity"]) < 1e-9:
            continue

        is_underlying = _is_underlying_symbol(symbol)
        greeks_per_unit = None
        expiry = None
        data_unavailable = []
        if not is_underlying:
            quote = option_chain.get(symbol) if option_chain is not None else None
            if quote is not None:
                expiry = quote.expiry
            if quote is not None and quote.greeks is not None:
                greeks_per_unit = quote.greeks
            else:
                data_unavailable.append("greeks_per_unit")
                if quote is None:
                    warnings.append(
                        f"{symbol} ({strategy_tag}): se reconstruyo la posicion "
                        f"(qty={remaining['quantity']:g}) pero no existe en el option_chain "
                        "actual (probable base fuera del universo activo: vencida o rolleada) "
                        "- ni expiry ni greeks_per_unit se pueden completar; "
                        "build_exit_signals() va a saltear esta posicion por completo hasta "
                        "que se cierre a mano (ver ggal_bot/ops/manual_close.py)."
                    )
                else:
                    warnings.append(
                        f"{symbol} ({strategy_tag}): se reconstruyo la posicion "
                        f"(qty={remaining['quantity']:g}) - esta en el universo activo (expiry "
                        "recuperado de la definicion del instrumento) pero sin punta de dos "
                        "lados vigente para calcular IV/griegas ahora mismo - greeks_per_unit "
                        "queda en None (Position.contribution() la trata como delta=1 por "
                        "unidad hasta que una entrada nueva la reemplace o se cierre)."
                    )

        entry_time = remaining["entry_time"]
        if hasattr(entry_time, "to_pydatetime"):
            entry_time = entry_time.to_pydatetime()

        new_pos = Position(
            symbol=symbol, quantity=remaining["quantity"],
            multiplier=1.0 if is_underlying else mult,
            greeks_per_unit=greeks_per_unit, expiry=expiry,
            entry_price=remaining["price"], entry_time=entry_time,
            strategy_tag=None if strategy_tag == "weekly_asymmetric" else strategy_tag,
        )
        # position_id (fix del "BUG B" - ver docstring del modulo): se
        # REUSA el del evento mas reciente del journal para este (symbol,
        # strategy_tag) en vez del UUID fresco que Position() genero por
        # default_factory - de ahora en mas, un restart ya no le inventa
        # una identidad nueva a una posicion que el journal ya conoce.
        new_pos.position_id = remaining["position_id"]
        new_pos.contract_key = (
            f"{SETTINGS.instruments.underlying_symbol}|{symbol}|{expiry.isoformat()}"
            if expiry is not None else None
        )
        positions.append(new_pos)
        if data_unavailable:
            logger.warning(
                "Reconciliacion de arranque (Event Journal): %s (%s) reconstruido con campos "
                "incompletos (%s).", symbol, strategy_tag, ", ".join(data_unavailable),
            )

    return positions, warnings


def _to_numeric(series):
    """pd.to_numeric local (evita un import de pandas a nivel de modulo en
    un archivo que, por lo demas, no lo necesita - mismo criterio de
    dependencia perezosa que el resto de este modulo)."""
    import pandas as pd
    return pd.to_numeric(series, errors="coerce")
