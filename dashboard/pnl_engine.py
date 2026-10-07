"""
pnl_engine.py
==============
Motor de consolidacion de PnL para el dashboard (dashboard/app.py). Lee
logs/shadow_trades.csv (el mismo CSV que escribe
ggal_bot.execution.order_gateway.ShadowAuditLogger en modo Shadow Trading)
y state/bot_state.json (el mismo JSON que escribe ggal_bot.state_writer,
ahora incluyendo un snapshot de la cadena de opciones - ver run_bot.py,
_option_chain_snapshot()), y produce:

    - Trades cerrados: aparea compras y ventas del mismo simbolo con una
      cola FIFO (deque por simbolo) para calcular el PnL realizado de cada
      round-trip.
    - Posiciones abiertas: lo que quedo sin aparear, marcado a mercado con
      la ultima cotizacion vigente del snapshot (o None si esa base ya no
      esta en la cadena vigente - ej. vencio o rodo fuera del universo).
    - Metricas de portafolio: PnL total (realizado + no realizado), win
      rate, profit factor, curva de equity, Sharpe aproximado y max
      drawdown.

IMPORTANTE - alcance y limitaciones (leer antes de confiar en los numeros):

    1. Esto consolida el PnL de las ordenes que EL BOT genero (via
       order_gateway.py), no una conciliacion contra el estado de cuenta
       real de un ALYC. En modo Shadow Trading no existe tal cuenta (ver
       ggal_bot/data/live_shadow_feed.py); en modo real, el PnL "de verdad"
       siempre debe validarse contra get_account_positions() y los
       resumenes de cuenta del broker, no solo contra este CSV.
    2. BUG REAL CORREGIDO (2026-09-29, ver REPORT.md SS12.0): `classify_strategy(symbol)`
       clasificaba TODO simbolo de opcion (sin importar cual estrategia lo
       abrio realmente) como `"vol_arbitrage"` - la premisa que justificaba
       esto ("la UNICA fuente de ordenes sobre opciones es
       VolatilityArbitrageStrategy") dejo de ser cierta hace tiempo:
       `weekly_asymmetric` y `scalping` tambien operan opciones
       directamente. Verificado por comparacion fila a fila: de 577 trades
       del export historico etiquetado "vol_arbitrage", 411 resultaron ser
       duplicados exactos de trades de `scalping`/`weekly_asymmetric` mal
       etiquetados. `classify_strategy()` se deja tal cual (varios tests
       existentes dependen de su comportamiento historico y ningun otro
       llamador de produccion consume el campo `strategy` que produce -
       ver `ggal_bot/ops/manual_close.py` y `ggal_bot/portfolio/
       reconciliation.py`, que solo usan `open_lots`, nunca `closed`) -
       este bug NUNCA afecto el riesgo en vivo, solo el reporting/dashboard.
       El camino correcto para el dashboard/exports es
       `classify_strategy_from_journal()` (mas abajo), que cruza cada fill
       contra `logs/position_events.csv` (Position Lifecycle Event
       Journal, ver `ggal_bot/portfolio/event_journal.py`) por
       `client_order_id` == `order_client_id` - el MISMO id de orden que
       ambos archivos comparten (ver `state.request.client_order_id` en
       `run_bot.py::_act_on_entry_signal`/`_act_on_exit_signal`), no una
       aproximacion. Un fill sin match en el journal (tipicamente: anterior
       al deploy de `event_journal.py`, 2026-09-07 17:05 UTC, commit
       `b3397fd`) se etiqueta explicitamente `"unknown_legacy"` - NUNCA se
       vuelve a adivinar `"vol_arbitrage"` por default, para no repetir el
       mismo error.
    3. Sharpe aproximado: se calcula sobre la serie de retornos por trade
       cerrado (pnl_pct), SIN anualizar (la frecuencia de trades de este
       bot es demasiado irregular para una anualizacion estandar). Sirve
       para comparar configuraciones entre si, no como un Sharpe ratio
       anualizado comparable con benchmarks tradicionales.
    4. PnL % por trade se calcula sobre el "nocional" de esa pata
       (precio_entrada * cantidad * multiplicador), una aproximacion
       practica - las opciones vendidas en descubierto no tienen un
       concepto de "capital invertido" tan limpio como una accion larga.
    5. El multiplicador se resuelve POR SIMBOLO (ver multiplier_for_symbol()):
       1.0 para el subyacente/futuro de GGAL (acciones, sin multiplicador de
       contrato), `SETTINGS.instruments.option_multiplier` (100) para
       cualquier opcion. Antes de esta correccion se aplicaba un unico
       multiplicador global a TODOS los simbolos, lo que inflaba x100 el
       PnL de cada pata de delta-hedge sobre el subyacente - en una sesion
       con rehedgeos frecuentes, eso dominaba el PnL Total mostrado en el
       dashboard (bug real reportado por el usuario: PnL Total de ~$1.670
       millones sobre un CSV que solo sostenia unos pocos millones de PnL
       realizado). Ver test_dashboard_pnl.py.
"""

from __future__ import annotations

import csv
import json
import math
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ggal_bot.config import SETTINGS
from ggal_bot.execution.order_gateway import ShadowAuditLogger
from ggal_bot.paths import POSITION_EVENTS_LOG, SHADOW_TRADES_LOG, STATE_FILE
from dashboard.data import load_errors

FILLS_COLUMNS = [
    "timestamp_utc", "client_order_id", "symbol", "side", "order_type",
    "quantity", "requested_price", "fill_price", "reference_price", "event",
]

POSITION_EVENTS_COLUMNS = [
    "timestamp_utc", "event_type", "position_id", "contract_key", "symbol",
    "strategy_tag", "side", "quantity_delta", "quantity_after", "price",
    "order_client_id", "reason", "data_unavailable_fields",
]

UNKNOWN_LEGACY_STRATEGY = "unknown_legacy"


# ---------------------------------------------------------------------------
# Carga de datos crudos
# ---------------------------------------------------------------------------

def _discover_shadow_trade_files(base_path: Path) -> list:
    """
    MEJORA 2026-10-05 (a pedido explicito del usuario, parte del fix de
    rotacion de schema - ver ggal_bot/execution/order_gateway.py::
    ShadowAuditLogger._resolve_write_path): desde ese fix, un cambio de
    schema hace que el logger rote a un archivo sibling nuevo
    (`{stem}.schemaN{suffix}`) en vez de seguir agregando filas a un header
    que no las describe - el archivo base NUNCA se reescribe. Esta funcion
    devuelve [base_path] + todo sibling de schema que exista, en orden de N
    ascendente (= orden cronologico real de rotacion), para que load_fills()
    pueda leer el historico COMPLETO sin que el llamador tenga que saber
    cuantas rotaciones hubo.
    """
    found = []
    if base_path.exists():
        found.append(base_path)
    n = 2
    while True:
        candidate = base_path.parent / f"{base_path.stem}.schema{n}{base_path.suffix}"
        if not candidate.exists():
            break
        found.append(candidate)
        n += 1
    return found


def _read_shadow_trades_file(path: Path) -> pd.DataFrame:
    """
    Lee UN archivo de shadow_trades (base o rotado) con pandas.read_csv()
    normal. Si eso falla con ParserError, cae a un parseo tolerante fila a
    fila - este fallback SOLO es necesario para un archivo escrito ANTES
    del fix de rotacion de schema (ver _discover_shadow_trade_files), que
    puede tener filas mas anchas que su propio header (el bug real
    encontrado en produccion: header viejo de 10 columnas + filas nuevas de
    13). Un archivo escrito DESPUES del fix nunca llega a este fallback -
    cada archivo de la familia es internamente consistente desde su
    primera fila.

    BUG REAL CORREGIDO (2026-10-05): antes, esta excepcion (y EmptyDataError)
    se atrapaba y la funcion devolvia un DataFrame vacio EN SILENCIO - sin
    loguear nada, sin avisar en la UI. Ahora se registra con
    dashboard.data.load_errors.register() antes de devolver el resultado
    tolerante (o vacio, si ni el fallback puede leerlo), para que
    dashboard/app.py pueda mostrar un banner visible con el archivo y el
    motivo exacto en vez de un silencioso "no hay operaciones".
    """
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=FILLS_COLUMNS)
    except pd.errors.ParserError as exc:
        load_errors.register(
            str(path),
            f"ParserError: {exc} - probablemente un archivo escrito antes del fix de "
            "rotacion de schema (header desactualizado + filas de un schema nuevo en el "
            "mismo archivo). Se intenta un parseo tolerante fila a fila; verificar "
            "igual, puede haber perdido columnas nuevas (bid_at_fill/ask_at_fill/mid_at_fill) "
            "para las filas mas viejas.",
        )
        canonical_header = ShadowAuditLogger._HEADER
        rows = []
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)  # descarta el header del archivo (ya sabemos que esta desactualizado)
            for row in reader:
                if len(row) < len(canonical_header):
                    row = row + [None] * (len(canonical_header) - len(row))
                elif len(row) > len(canonical_header):
                    row = row[: len(canonical_header)]
                rows.append(row)
        return pd.DataFrame(rows, columns=canonical_header)


def load_fills(csv_path: Optional[Path] = None) -> pd.DataFrame:
    """
    Lee logs/shadow_trades.csv (+ cualquier sibling rotado por cambio de
    schema, ver _discover_shadow_trade_files) y devuelve solo las filas de
    fill (event == 'shadow_fill'), ordenadas cronologicamente. Tolerante a
    que el archivo todavia no exista (el bot nunca corrio) o este vacio -
    ESO es un caso legitimo y silencioso. Un archivo que existe, tiene
    contenido, y falla al parsear YA NO es silencioso (ver
    _read_shadow_trades_file y dashboard.data.load_errors).
    """
    base_path = Path(csv_path) if csv_path is not None else SHADOW_TRADES_LOG
    files = _discover_shadow_trade_files(base_path)
    if not files:
        return pd.DataFrame(columns=FILLS_COLUMNS)

    frames = []
    for path in files:
        if path.stat().st_size == 0:
            continue
        frames.append(_read_shadow_trades_file(path))

    if not frames:
        return pd.DataFrame(columns=FILLS_COLUMNS)
    df = pd.concat(frames, ignore_index=True)

    if df.empty or "event" not in df.columns:
        return pd.DataFrame(columns=FILLS_COLUMNS)

    df = df[df["event"] == "shadow_fill"].copy()
    if df.empty:
        return df

    # BUG REAL VERIFICADO (2026-09-08, ver ggal_bot/ops/manual_close.py y su
    # test_manual_close.py::test_close_position_manually_auto_detects...):
    # sin format="ISO8601", pandas (>=2.0) infiere el formato de fecha del
    # PRIMER valor no nulo de la columna y lo aplica de forma estricta al
    # resto - un timestamp_utc con distinta precision de sub-segundo (ej.
    # "...T10:00:00+00:00" sin microsegundos vs "...T10:00:00.123456+00:00"
    # con microsegundos, ambos producidos por datetime.now(timezone.utc).
    # isoformat() en ShadowAuditLogger.log_fill segun si el microsegundo
    # cae en 0) se parsea como NaT en silencio - y dropna() lo descarta sin
    # ningun warning. Reproducido de forma determinista: ver
    # test_load_fills_parses_mixed_subsecond_precision_timestamps_without_dropping_rows.
    # Esto corre el riesgo real de perder fills completos (compras o
    # ventas) tanto en el dashboard como en la reconciliacion de arranque
    # del bot (reconstruct_positions_from_shadow_log usa esta misma
    # funcion) - format="ISO8601" parsea cada valor de forma independiente
    # (sigue siendo estrictamente ISO 8601, no "adivina" formatos raros).
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce", format="ISO8601")
    for col in ("quantity", "fill_price", "reference_price", "requested_price"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["timestamp_utc", "quantity", "fill_price", "symbol", "side"])
    df = df.sort_values("timestamp_utc").reset_index(drop=True)
    return df


def load_position_events(csv_path: Optional[Path] = None) -> pd.DataFrame:
    """
    Lee logs/position_events.csv (Position Lifecycle Event Journal, ver
    ggal_bot/portfolio/event_journal.py) - archivo NUEVO e independiente de
    shadow_trades.csv, solo tiene eventos desde su deploy (2026-09-07 17:05
    UTC, commit b3397fd) en adelante, no reconstruye historial previo (ver
    docstring de ese modulo). Tolerante a que el archivo todavia no exista
    o este vacio - MISMO criterio que load_fills(), reutilizado aca para no
    duplicar la carga entre dashboard/app.py (pestaña "Lifecycle") y
    classify_strategy_from_journal() (mas abajo), que la necesitan ambas.
    """
    path = Path(csv_path) if csv_path is not None else POSITION_EVENTS_LOG
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=POSITION_EVENTS_COLUMNS)

    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=POSITION_EVENTS_COLUMNS)
    except pd.errors.ParserError as exc:
        # BUG REAL CORREGIDO (2026-10-05, a pedido explicito del usuario -
        # auditoria completa de loaders tras encontrar el caso real en
        # load_fills()): antes, esto se atrapaba y devolvia vacio en
        # silencio. position_events.csv no tiene el bug de schema de
        # shadow_trades.csv (su header nunca cambio), pero si este archivo
        # alguna vez se corrompe por otro motivo, que quede visible en vez
        # de leerse como "no hay eventos".
        load_errors.register(str(path), f"ParserError: {exc}")
        return pd.DataFrame(columns=POSITION_EVENTS_COLUMNS)

    if df.empty:
        return pd.DataFrame(columns=POSITION_EVENTS_COLUMNS)

    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce", format="ISO8601")
    return df


def build_order_client_id_strategy_map(events_df: pd.DataFrame) -> Dict[str, str]:
    """
    Devuelve {order_client_id: strategy_tag} a partir del event journal -
    la clave de cruce EXACTA (no aproximada) entre un fill de
    shadow_trades.csv (columna client_order_id) y el evento de lifecycle
    que lo origino (columna order_client_id): ambos son el MISMO
    client_order_id de la orden real (ver run_bot.py::_act_on_entry_signal/
    _act_on_exit_signal, que pasan
    `order_client_id=state.request.client_order_id` al loguear el evento -
    el mismo `state.request.client_order_id` que
    ShadowAuditLogger.log_fill() ya persiste como `client_order_id` en
    shadow_trades.csv).

    Filas sin order_client_id (ej. REJECT: nunca llega a haber fill, asi
    que no tiene orden real para cruzar) o sin strategy_tag se ignoran. Si
    el mismo order_client_id aparece en mas de una fila (ej. ENTRY seguido
    de un CLOSE posterior de la misma orden) se usa la PRIMERA con datos
    validos - alcanza con una, y todas las filas de una misma orden
    comparten el mismo strategy_tag por construccion.
    """
    mapping: Dict[str, str] = {}
    if events_df.empty or "order_client_id" not in events_df.columns:
        return mapping
    for row in events_df.itertuples(index=False):
        client_id = str(getattr(row, "order_client_id", "") or "").strip()
        strategy_tag = str(getattr(row, "strategy_tag", "") or "").strip()
        if not client_id or not strategy_tag:
            continue
        mapping.setdefault(client_id, strategy_tag)
    return mapping


def build_order_client_id_position_map(events_df: pd.DataFrame) -> Dict[str, str]:
    """
    Devuelve {order_client_id: position_id} a partir del event journal - el
    MISMO cruce exacto que build_order_client_id_strategy_map (mismo
    client_order_id/order_client_id compartido entre shadow_trades.csv y
    position_events.csv), pero para el Position ID en vez del strategy_tag.

    USADO POR match_trades_fifo() (via resolve_position_ids_from_journal())
    para agrupar los lotes FIFO por POSICION REAL en vez de solo por
    simbolo - ver el docstring de esa funcion para el bug real que esto
    corrige (2026-09-30, verificado con export-lifecycle.csv: la posicion
    weekly_asymmetric `9bf25bc4c8ca` sobre GFGC7400OC quedo con 7 contratos
    sin cerrar dentro del export; el simbolo-only FIFO le asigno esos 7
    contratos, mas viejos en la cola, a la PRIMERA venta de `scalping` que
    llego despues sobre el mismo simbolo, en vez de a la propia compra de
    scalping - cruzando PnL real entre ambas estrategias).

    Filas sin order_client_id o sin position_id se ignoran (igual que la
    funcion hermana). Si el mismo order_client_id aparece en mas de una
    fila se usa la PRIMERA con datos validos.
    """
    mapping: Dict[str, str] = {}
    if events_df.empty or "order_client_id" not in events_df.columns:
        return mapping
    for row in events_df.itertuples(index=False):
        client_id = str(getattr(row, "order_client_id", "") or "").strip()
        position_id = str(getattr(row, "position_id", "") or "").strip()
        if not client_id or not position_id:
            continue
        mapping.setdefault(client_id, position_id)
    return mapping


_MANUAL_CLOSE_PREFIX = "manual-close-"
_MANUAL_CLOSE_FALLBACK_TOLERANCE_SECONDS = 5.0


def _manual_close_fallback_matches(
    fills: pd.DataFrame, events_df: pd.DataFrame,
) -> Dict[Any, Tuple[str, str]]:
    """
    FIX 2026-10-07 (a pedido explicito del usuario, "matching de respaldo
    para manual-close-*" - ver tambien el fix de
    ggal_bot/ops/manual_close.py del 2026-10-06): cubre el caso de un fill
    con `client_order_id="manual-close-XXXXXXXX"` que NO tiene match
    EXACTO en el event journal porque fue escrito ANTES de ese fix, cuando
    `close_position_manually_from_journal()` generaba DOS uuid.uuid4()
    independientes - uno para el evento CLOSE del journal
    (`order_client_id`) y otro para el fill de shadow_trades.csv
    (`client_order_id`) - en vez de uno solo compartido.

    Caso real de produccion que motiva esto (posicion `ef268fed9a99`,
    GFGV5000OC, 2026-10-02): evento CLOSE con
    `order_client_id=manual-close-b71559e4`, fill con
    `client_order_id=manual-close-c64e13c1` - dos ids DISTINTOS para la
    MISMA operacion de cierre, verificado leyendo ambos archivos. Sin este
    fallback, esa pata queda invisible para el lado FIFO de la
    reconciliacion del dashboard para siempre (el fix de manual_close.py
    evita que esto le pase a un cierre manual FUTURO, pero no puede
    reescribir un id que YA quedo grabado en el historial - esto NUNCA
    reescribe ningun CSV, solo resuelve la clasificacion/agrupacion en
    memoria para el calculo de PnL).

    Estrategia (sin fabricar nada - solo evidencia circunstancial fuerte,
    nunca una garantia absoluta, por eso es un FALLBACK, no el camino
    principal): para cada fill cuyo `client_order_id` empieza con
    "manual-close-" y NO tiene match exacto en ningun evento del journal
    (huerfano real: su id no aparece como `order_client_id` de NINGUNA
    fila), busca un evento CLOSE del journal que TAMBIEN tenga
    `order_client_id` con ese prefijo y TAMBIEN sea huerfano (su id no
    aparece como `client_order_id` de NINGUN fill - si apareciera, ese
    evento ya tiene su propio match exacto legitimo en otro fill y nunca
    se le "presta" a este), del MISMO simbolo, dentro de una ventana de
    tiempo chica (`_MANUAL_CLOSE_FALLBACK_TOLERANCE_SECONDS` segundos) - el
    evento del journal y el fill del mismo cierre manual se escriben a
    milisegundos de diferencia en la MISMA llamada de funcion (dos
    escrituras consecutivas, ver close_position_manually_from_journal),
    nunca minutos. Si hay mas de un candidato del mismo simbolo dentro de
    la ventana (mas de un cierre manual historico del mismo simbolo), cada
    fill (en orden cronologico) se aparea con el candidato MAS CERCANO en
    el tiempo todavia disponible - matching greedy, un evento del journal
    nunca se reusa para mas de un fill.

    Devuelve {indice_de_fills: (strategy_tag, position_id)} SOLO para los
    fills que encontraron un candidato dentro de la ventana - un fill sin
    candidato compatible queda sin entrada (el llamador sigue tratandolo
    como "unknown_legacy"/"" igual que antes de este fallback, nunca se
    fabrica un match sin evidencia).
    """
    matches: Dict[Any, Tuple[str, str]] = {}
    if fills.empty or events_df.empty:
        return matches
    if "order_client_id" not in events_df.columns or "client_order_id" not in fills.columns:
        return matches

    all_fill_client_ids = {
        str(v).strip() for v in fills["client_order_id"] if str(v).strip()
    }
    all_event_client_ids = {
        str(getattr(row, "order_client_id", "") or "").strip()
        for row in events_df.itertuples(index=False)
    }
    all_event_client_ids.discard("")

    candidates: List[Dict[str, Any]] = []
    for row in events_df.itertuples(index=False):
        if str(getattr(row, "event_type", "") or "") != "CLOSE":
            continue
        order_client_id = str(getattr(row, "order_client_id", "") or "").strip()
        if not order_client_id.startswith(_MANUAL_CLOSE_PREFIX):
            continue
        if order_client_id in all_fill_client_ids:
            continue  # tiene match exacto en algun fill - no es huerfano, no se toca
        ts = getattr(row, "timestamp_utc", None)
        if ts is None or pd.isna(ts):
            continue
        candidates.append({
            "symbol": str(getattr(row, "symbol", "") or ""),
            "timestamp": ts,
            "strategy_tag": str(getattr(row, "strategy_tag", "") or ""),
            "position_id": str(getattr(row, "position_id", "") or ""),
        })
    if not candidates:
        return matches

    unmatched_fills: List[Tuple[Any, str, Any]] = []
    for idx, row in zip(fills.index, fills.itertuples(index=False)):
        client_id = str(getattr(row, "client_order_id", "") or "").strip()
        if not client_id.startswith(_MANUAL_CLOSE_PREFIX):
            continue
        if client_id in all_event_client_ids:
            continue  # tiene match exacto en el journal - no necesita fallback
        ts = getattr(row, "timestamp_utc", None)
        if ts is None or pd.isna(ts):
            continue
        unmatched_fills.append((idx, str(getattr(row, "symbol", "") or ""), ts))
    if not unmatched_fills:
        return matches

    unmatched_fills.sort(key=lambda t: t[2])
    tolerance = pd.Timedelta(seconds=_MANUAL_CLOSE_FALLBACK_TOLERANCE_SECONDS)
    used_candidates: set = set()

    for fill_idx, symbol, ts in unmatched_fills:
        best_ci = None
        best_diff = None
        for ci, cand in enumerate(candidates):
            if ci in used_candidates or cand["symbol"] != symbol:
                continue
            diff = abs(cand["timestamp"] - ts)
            if diff > tolerance:
                continue
            if best_diff is None or diff < best_diff:
                best_ci, best_diff = ci, diff
        if best_ci is not None:
            used_candidates.add(best_ci)
            cand = candidates[best_ci]
            matches[fill_idx] = (cand["strategy_tag"], cand["position_id"])

    return matches


def resolve_position_ids_from_journal(
    fills: pd.DataFrame, events_df: Optional[pd.DataFrame] = None,
) -> pd.Series:
    """
    Devuelve una Serie alineada al indice de `fills` con el Position ID real
    (desde el event journal, cruzado por client_order_id) para cada fill, o
    "" si no hay match (fill anterior al deploy del journal, o de una
    estrategia que todavia no loguea sus ENTRY - ver Prioridad 2 en curso
    para vol_arbitrage/delta_hedge). Para un fill "manual-close-*" sin
    match exacto, intenta primero el fallback por cercania (ver
    _manual_close_fallback_matches) antes de rendirse con "".

    match_trades_fifo() usa esto (cuando el llamador lo agrega como columna
    "position_id" de `fills`, mismo patron que fills["strategy"] via
    classify_strategy_from_journal()) para agrupar los lotes FIFO por
    posicion real en vez de por simbolo+estrategia - ver docstring de
    match_trades_fifo para el detalle completo de la jerarquia de fallback.
    """
    if events_df is None:
        events_df = load_position_events()
    if fills.empty:
        return pd.Series([], dtype=object, index=fills.index)
    position_map = build_order_client_id_position_map(events_df)
    fallback_matches = _manual_close_fallback_matches(fills, events_df)

    def _resolve_row(idx, row) -> str:
        client_id = str(getattr(row, "client_order_id", "") or "").strip()
        if client_id in position_map:
            return position_map[client_id]
        fallback = fallback_matches.get(idx)
        if fallback and fallback[1]:
            return fallback[1]
        return ""

    return pd.Series(
        [_resolve_row(idx, row) for idx, row in zip(fills.index, fills.itertuples(index=False))],
        index=fills.index,
    )


def classify_strategy_from_journal(
    fills: pd.DataFrame, events_df: Optional[pd.DataFrame] = None,
) -> pd.Series:
    """
    Clasificacion de estrategia CORREGIDA (2026-09-29, ver REPORT.md
    SS12.0) para el dashboard/exports: reemplaza a `fills["symbol"].apply
    (classify_strategy)` (el bug real ya documentado - ver docstring del
    modulo, punto 2). Devuelve una Serie alineada al indice de `fills`.

    Regla, en orden:
      1. Subyacente/futuro -> "delta_hedge" (esto SI era confiable con solo
         el simbolo - DeltaHedgingEngine es la unica fuente de ordenes
         sobre el subyacente - y sigue siendo confiable ahora).
      2. Opcion CON match de client_order_id en el event journal -> el
         strategy_tag REAL de ese match (weekly_asymmetric/scalping/
         vol_arbitrage/lo que sea).
      3. Opcion "manual-close-*" SIN match exacto -> intenta el fallback
         por cercania de simbolo+tiempo (ver _manual_close_fallback_matches,
         FIX 2026-10-07) antes de rendirse.
      4. Opcion SIN match (tipicamente anterior al deploy del journal, o
         sin candidato de fallback compatible) -> "unknown_legacy", NUNCA
         "vol_arbitrage" por default - no hay evidencia para adivinar cual
         estrategia la abrio, y adivinar mal es exactamente el bug que esto
         corrige.

    Si `events_df` es None, se carga con load_position_events() (comodo
    para el llamador tipico de dashboard/app.py); pasarlo explicitamente
    evita releer el archivo dos veces cuando el llamador ya lo tiene (ej.
    para la pestaña "Lifecycle").
    """
    if events_df is None:
        events_df = load_position_events()
    if fills.empty:
        return pd.Series([], dtype=object, index=fills.index)
    strategy_map = build_order_client_id_strategy_map(events_df)
    fallback_matches = _manual_close_fallback_matches(fills, events_df)

    def _classify_row(idx, row) -> str:
        symbol = getattr(row, "symbol", "")
        if _is_underlying_symbol(symbol):
            return "delta_hedge"
        client_id = str(getattr(row, "client_order_id", "") or "").strip()
        if client_id in strategy_map:
            return strategy_map[client_id]
        fallback = fallback_matches.get(idx)
        if fallback and fallback[0]:
            return fallback[0]
        return UNKNOWN_LEGACY_STRATEGY

    return pd.Series(
        [_classify_row(idx, row) for idx, row in zip(fills.index, fills.itertuples(index=False))],
        index=fills.index,
    )


def load_bot_state(path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Lee state/bot_state.json (ver ggal_bot/state_writer.py). Devuelve {} si
    el archivo todavia no existe o esta a medio escribir (el bot lo escribe
    de forma atomica - tmp + replace -, asi que esto solo pasa en una
    ventana de carrera muy angosta; el proximo refresh del dashboard lo
    vuelve a leer bien).
    """
    state_path = Path(path) if path is not None else STATE_FILE
    if not state_path.exists():
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        # BUG REAL CORREGIDO (2026-10-05, a pedido explicito del usuario):
        # antes esto era silencioso. La ventana de carrera descrita arriba
        # (bot escribiendo con tmp+replace) es benigna y se autocorrige en
        # el proximo refresh - pero si este error se repite en CADA
        # refresh, es una señal real de un bot_state.json corrupto/
        # inaccesible, y antes no habia forma de distinguirlo de "el bot
        # nunca escribio nada" sin mirar los logs del proceso a mano.
        load_errors.register(
            str(state_path),
            f"{type(exc).__name__}: {exc} (si persiste en varios refreshes, no es la "
            "ventana de carrera esperada de la escritura atomica - revisar el archivo)",
        )
        return {}


# ---------------------------------------------------------------------------
# Clasificacion de estrategia (ver nota 2 en el docstring del modulo)
# ---------------------------------------------------------------------------

def _underlying_symbol_aliases() -> frozenset:
    """
    Todos los "alias" de simbolo conocidos para el subyacente/futuro de GGAL
    - no solo el ticker completo y calificado (`contado_ticker`,
    "MERV - XMEV - GGAL - 24hs"), sino tambien el ticker corto
    (`underlying_symbol`, "GGAL") que puede llegar asi desde otra fuente de
    datos o de un fill simulado en tests.

    BUG REAL CORREGIDO (auditoria del 2026-08-27, ver
    docs/AUDITORIA_MAESTRA_2026-08-27.md seccion 3.6): classify_strategy() y
    multiplier_for_symbol() comparaban el simbolo por IGUALDAD EXACTA contra
    un unico string (`cfg.contado_ticker`). Un fill que llegara con el
    simbolo corto "GGAL" (en vez del ticker completo) no matcheaba nada, y
    caia por default al multiplicador de OPCIONES (100) en vez de 1.0 -
    reintroduciendo, para esa variante de simbolo, exactamente el mismo bug
    x100 que `multiplier_for_symbol()` ya habia corregido para el ticker
    canonico (ver docstring de esa funcion). Fix: comparar contra un
    CONJUNTO de alias conocidos del subyacente, no un unico string.
    """
    cfg = SETTINGS.instruments
    return frozenset(s for s in (cfg.contado_ticker, cfg.futuro_ticker, cfg.underlying_symbol) if s)


def _is_underlying_symbol(symbol: str) -> bool:
    return symbol in _underlying_symbol_aliases()


def classify_strategy(symbol: str) -> str:
    if _is_underlying_symbol(symbol):
        return "delta_hedge"
    return "vol_arbitrage"


def multiplier_for_symbol(symbol: str, option_multiplier: Optional[float] = None) -> float:
    """
    1.0 para el subyacente/futuro (acciones/futuro de GGAL: sin multiplicador
    de contrato de opcion), `option_multiplier` (default: SETTINGS.instruments.
    option_multiplier, 100) para cualquier otro simbolo (una opcion).

    BUG REAL CORREGIDO ACA (reportado por el usuario via el dashboard: PnL
    Total mostraba ~$1.670 millones cuando el CSV de fills solo sostenia
    unos pocos millones de PnL realizado): antes, match_trades_fifo() y
    mark_to_market() aplicaban un UNICO multiplicador global (el de
    opciones, 100) a TODOS los simbolos - incluidas las patas de
    delta-hedge sobre el subyacente (`SETTINGS.instruments.contado_ticker`,
    "MERV - XMEV - GGAL - 24hs"), que son acciones, no contratos de
    opciones de 100 unidades. Eso inflaba x100 el PnL (realizado Y no
    realizado) de CADA pata de delta-hedge, que en un dia de rehedgeos
    frecuentes domina el total. Ver test_dashboard_pnl.py,
    test_multiplier_for_symbol_* y test_match_trades_fifo_uses_multiplier_1_for_delta_hedge_legs.

    Ver tambien _underlying_symbol_aliases() arriba: la comparacion original
    (un unico string exacto) tenia el mismo bug de clase para variantes de
    simbolo del subyacente (ej. "GGAL" a secas) - ya corregido aca.
    """
    default = option_multiplier if option_multiplier is not None else SETTINGS.instruments.option_multiplier
    if _is_underlying_symbol(symbol):
        return 1.0
    return default


def classify_option_type(symbol: str) -> str:
    """Call/Put/Subyacente, por prefijo de simbolo - solo para filtros del dashboard."""
    cfg = SETTINGS.instruments
    if _is_underlying_symbol(symbol):
        return "subyacente"
    bare = symbol.split(" - ")[2].strip() if symbol.count(" - ") >= 2 else symbol
    if bare.startswith(cfg.call_prefix):
        return "call"
    if bare.startswith(cfg.put_prefix):
        return "put"
    return "otro"


# ---------------------------------------------------------------------------
# Apareo FIFO de compras/ventas -> trades cerrados + posiciones abiertas
# ---------------------------------------------------------------------------

@dataclass
class ClosedTrade:
    symbol: str
    strategy: str
    direction: str            # "long" (el bot compro primero) o "short" (vendio primero)
    quantity: float            # contratos apareados en este cierre (siempre positivo)
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    entry_order_id: str
    exit_order_id: str
    pnl_ars: float
    pnl_pct: float
    holding_seconds: float


@dataclass
class OpenLot:
    symbol: str
    strategy: str
    quantity: float            # signed: + long, - short
    entry_time: pd.Timestamp
    entry_price: float
    entry_order_id: str


def match_trades_fifo(
    fills: pd.DataFrame, option_multiplier: Optional[float] = None,
) -> Tuple[List[ClosedTrade], List[OpenLot]]:
    """
    Recorre los fills en orden cronologico y aparea, por simbolo, cada
    fill contra los lotes abiertos de signo OPUESTO en una cola FIFO (el
    lote mas viejo se cierra primero). Lo que no se puede aparear (porque
    no hay lotes opuestos, o porque sobra cantidad) queda como un nuevo
    lote abierto.

    El multiplicador se resuelve POR SIMBOLO (ver multiplier_for_symbol()):
    1.0 para el subyacente/futuro (acciones), `option_multiplier` para
    cualquier opcion - nunca un unico valor global para todos los simbolos
    (ver nota de bug real en multiplier_for_symbol()).

    BUG REAL CORREGIDO ACA (2026-09-30, encontrado al construir el panel de
    reconciliacion del dashboard - ver REPORT.md): esta funcion ignoraba
    por completo una columna "strategy" ya presente en `fills` y volvia a
    calcular la estrategia de CADA fila llamando a classify_strategy(symbol)
    -EL BUG ORIGINAL, siempre "vol_arbitrage" para cualquier opcion-, aunque
    dashboard/app.py YA hubiera poblado fills["strategy"] con el valor
    CORRECTO via classify_strategy_from_journal() antes de llamar a esta
    funcion. En los hechos, la columna corregida quedaba calculada pero
    nunca se usaba: ClosedTrade.strategy/OpenLot.strategy (y por lo tanto
    la columna "Estrategia" de las tablas Cerradas/Abiertas, y el filtro de
    estrategia de la sidebar) seguian mostrando la clasificacion vieja y
    contaminada. Reproducido con un test antes de este fix (ver
    test_match_trades_fifo_uses_the_strategy_column_when_present, no
    "vol_arbitrage" adivinado). Ahora: si `fills` trae una columna
    "strategy", se usa tal cual (fuente unica de verdad = lo que el
    llamador ya clasifico); si no la trae (compatibilidad con llamadores
    viejos/tests que arman fills a mano), se cae al classify_strategy(symbol)
    de siempre - mismo comportamiento que antes SOLO para ese caso.

    SEGUNDO BUG REAL VERIFICADO Y CORREGIDO ACA (2026-09-30, a pedido
    explicito del usuario, medido contra export-lifecycle.csv - 1333 filas,
    2026-09-07 a 2026-09-28, ver analisis en la sesion): la cola FIFO
    (`open_lots`) se indexaba SOLO por `symbol`. Verificado con una
    reconstruccion fill-a-fill de ese export que esto SI cruzo PnL real
    entre estrategias: la posicion `weekly_asymmetric` `9bf25bc4c8ca` sobre
    GFGC7400OC (ENTRY +13 @153.001 el 2026-09-15, PARTIAL_EXIT -6 @202.500
    el 2026-09-16, quedan 7 contratos abiertos que NUNCA se cierran dentro
    del export) dejo un lote abierto mas viejo en la cola de ese simbolo.
    Cuando `scalping` opero el MISMO simbolo despues (2026-09-21 en
    adelante, ENTRY+CLOSE propios, un round-trip completo y correcto en si
    mismo), el FIFO symbol-only le asigno la venta de cierre de `scalping`
    contra el lote VIEJO de `weekly_asymmetric` (el mas antiguo en la cola),
    en vez de contra su propia compra - diferencia medida: ARS 1.361,50 de
    PnL mal atribuido entre ambas estrategias solo en ese simbolo (13,615
    unidades * multiplicador 100). Importante: esto NO requiere que las
    VENTANAS DE TIEMPO de ambas posiciones se solapen en el sentido
    tradicional (0 pares de posiciones CERRADAS se solapan en ese export) -
    alcanza con que una posicion quede SIN CERRAR (abierta) cuando otra
    estrategia opera el mismo simbolo despues, porque FIFO no sabe que ese
    lote "pertenece" a otra estrategia.

    FIX: la cola FIFO ahora se indexa por (symbol, match_key), con
    match_key resuelto en esta jerarquia de fallback (la misma que pidio el
    usuario: "emparejar por Position ID, o por simbolo + estrategia si el
    fill no trae Position ID"):
      1. Si `fills` trae una columna "position_id" (ver
         resolve_position_ids_from_journal(), cruce EXACTO por
         client_order_id contra logs/position_events.csv) Y esa fila tiene
         un valor no vacio -> match_key = f"pid:{position_id}". Esto es lo
         mas preciso posible: agrupa por la POSICION REAL, no por una
         aproximacion - corrige tambien el caso (documentado en
         AUDITORIA_FASE5.2_LIFECYCLE_ROOT_CAUSE.md) de dos posiciones de la
         MISMA estrategia sobre el mismo simbolo simultaneas por
         fragmentacion.
      2. Si no hay Position ID resuelto para esa fila (tipicamente: fill
         anterior al deploy del journal, o de una estrategia que todavia no
         loguea sus ENTRY al journal - vol_arbitrage/delta_hedge, Prioridad
         2 en curso) -> match_key = f"strat:{strategy}" (la misma columna
         "strategy" ya corregida arriba, via classify_strategy_from_journal
         o classify_strategy(symbol) de fallback).
    Backward-compatible: un llamador/test que no agrega ninguna columna
    nueva sigue viendo el comportamiento de ANTES de este fix (fallback 2
    con classify_strategy(symbol), que agrupa igual que "symbol-only" para
    fills de opciones - ver test_match_trades_fifo_falls_back_to_classify_strategy_when_column_absent).
    Ver test_match_trades_fifo_groups_fifo_lots_by_position_id_not_by_symbol_alone
    para la reproduccion exacta de este bug y su regresion.
    """
    open_lots: Dict[Tuple[str, str], deque] = {}
    closed: List[ClosedTrade] = []
    has_strategy_column = "strategy" in fills.columns
    has_position_id_column = "position_id" in fills.columns

    for row in fills.itertuples(index=False):
        symbol = row.symbol
        signed_qty = float(row.quantity) if row.side == "buy" else -float(row.quantity)
        strategy = row.strategy if has_strategy_column else classify_strategy(symbol)
        multiplier = multiplier_for_symbol(symbol, option_multiplier)

        position_id = str(getattr(row, "position_id", "") or "").strip() if has_position_id_column else ""
        match_key = f"pid:{position_id}" if position_id else f"strat:{strategy}"
        queue = open_lots.setdefault((symbol, match_key), deque())

        remaining = signed_qty
        while remaining != 0 and queue and (queue[0].quantity > 0) != (remaining > 0):
            lot = queue[0]
            match_qty = min(abs(lot.quantity), abs(remaining))
            direction = "long" if lot.quantity > 0 else "short"
            pnl_per_unit = (row.fill_price - lot.entry_price) if direction == "long" else (lot.entry_price - row.fill_price)
            pnl_ars = pnl_per_unit * match_qty * multiplier
            notional = lot.entry_price * match_qty * multiplier
            pnl_pct = (pnl_ars / notional * 100.0) if notional else 0.0

            closed.append(ClosedTrade(
                symbol=symbol, strategy=strategy, direction=direction, quantity=match_qty,
                entry_time=lot.entry_time, exit_time=row.timestamp_utc,
                entry_price=lot.entry_price, exit_price=row.fill_price,
                entry_order_id=lot.entry_order_id, exit_order_id=row.client_order_id,
                pnl_ars=pnl_ars, pnl_pct=pnl_pct,
                holding_seconds=(row.timestamp_utc - lot.entry_time).total_seconds(),
            ))

            if lot.quantity > 0:
                lot.quantity -= match_qty
                remaining += match_qty
            else:
                lot.quantity += match_qty
                remaining -= match_qty
            if lot.quantity == 0:
                queue.popleft()

        if remaining != 0:
            queue.append(OpenLot(
                symbol=symbol, strategy=strategy, quantity=remaining,
                entry_time=row.timestamp_utc, entry_price=row.fill_price,
                entry_order_id=row.client_order_id,
            ))

    open_positions = [lot for q in open_lots.values() for lot in q if lot.quantity != 0]
    return closed, open_positions


def aggregate_open_positions(open_lots: List[OpenLot]) -> pd.DataFrame:
    """Consolida lotes abiertos del mismo simbolo+signo en una sola fila (precio promedio ponderado)."""
    columns = ["symbol", "strategy", "quantity", "avg_entry_price", "entry_time"]
    if not open_lots:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame([lot.__dict__ for lot in open_lots])
    df["direction_sign"] = np.sign(df["quantity"])
    rows = []
    for (symbol, sign), group in df.groupby(["symbol", "direction_sign"]):
        qty = group["quantity"].sum()
        if qty == 0:
            continue
        avg_price = (group["quantity"] * group["entry_price"]).sum() / qty
        rows.append({
            "symbol": symbol,
            "strategy": group["strategy"].iloc[0],
            "quantity": qty,
            "avg_entry_price": avg_price,
            "entry_time": group["entry_time"].min(),
        })
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)


# ---------------------------------------------------------------------------
# Marca a mercado de posiciones abiertas
# ---------------------------------------------------------------------------

def get_current_price(symbol: str, bot_state: Dict[str, Any]) -> Optional[float]:
    """Ultimo mid conocido para `symbol` segun state/bot_state.json, o None si no esta disponible."""
    if not bot_state:
        return None
    cfg = SETTINGS.instruments
    if symbol in (cfg.contado_ticker, cfg.futuro_ticker):
        spot = (bot_state.get("extra") or {}).get("spot_mid")
        return float(spot) if spot is not None else None
    for q in bot_state.get("option_chain_snapshot", []) or []:
        if q.get("symbol") == symbol:
            mid = q.get("mid")
            return float(mid) if mid not in (None, 0) else None
    return None


def mark_to_market(
    open_positions_df: pd.DataFrame, bot_state: Dict[str, Any], option_multiplier: Optional[float] = None,
) -> pd.DataFrame:
    """
    Agrega current_price/pnl_ars/pnl_pct/has_current_price a cada fila de
    posiciones abiertas. El multiplicador se resuelve POR SIMBOLO (ver
    multiplier_for_symbol()): 1.0 para el subyacente/futuro, `option_multiplier`
    para opciones - nunca un unico valor global (ver nota de bug real en
    multiplier_for_symbol()).
    """
    columns = list(open_positions_df.columns) + ["current_price", "pnl_ars", "pnl_pct", "has_current_price"]
    if open_positions_df.empty:
        return pd.DataFrame(columns=columns)

    df = open_positions_df.copy()
    # pd.to_numeric convierte los None que devuelve get_current_price() en
    # NaN "de verdad" (float64) en vez de dejar la columna en dtype object
    # con Nones sueltos - object+None revienta mas adelante en cualquier
    # operacion numerica (ej. Series.round() en dashboard/app.py, que no
    # sabe redondear un NoneType).
    df["current_price"] = pd.to_numeric(
        df["symbol"].apply(lambda s: get_current_price(s, bot_state)), errors="coerce",
    )
    df["has_current_price"] = df["current_price"].notna()
    row_multiplier = df["symbol"].apply(lambda s: multiplier_for_symbol(s, option_multiplier))
    df["pnl_ars"] = np.where(
        df["has_current_price"],
        (df["current_price"] - df["avg_entry_price"]) * df["quantity"] * row_multiplier,
        0.0,
    )
    notional = (df["avg_entry_price"] * df["quantity"].abs() * row_multiplier).replace(0, np.nan)
    df["pnl_pct"] = (df["pnl_ars"] / notional * 100.0).fillna(0.0)
    return df


# ---------------------------------------------------------------------------
# Metricas de portafolio
# ---------------------------------------------------------------------------

def compute_equity_curve(closed_trades: List[ClosedTrade]) -> pd.DataFrame:
    """Curva de equity acumulada (solo PnL REALIZADO, ordenado por momento de cierre)."""
    if not closed_trades:
        return pd.DataFrame(columns=["timestamp", "pnl_ars", "cumulative_pnl_ars"])
    df = pd.DataFrame([{"timestamp": t.exit_time, "pnl_ars": t.pnl_ars} for t in closed_trades])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["cumulative_pnl_ars"] = df["pnl_ars"].cumsum()
    return df


def compute_max_drawdown(equity_curve: pd.DataFrame) -> Dict[str, Optional[float]]:
    if equity_curve.empty:
        return {"max_drawdown_ars": 0.0, "max_drawdown_pct": None}
    running_max = equity_curve["cumulative_pnl_ars"].cummax()
    drawdown = equity_curve["cumulative_pnl_ars"] - running_max
    max_dd_ars = float(drawdown.min())
    idx = drawdown.idxmin()
    peak_at_min = float(running_max.loc[idx])
    # El % de drawdown solo tiene sentido cuando el equity acumulado llego a
    # estar en positivo antes de la caida (si el "pico" fue <= 0, dividir
    # por el da un porcentaje sin sentido economico).
    max_dd_pct = (max_dd_ars / peak_at_min * 100.0) if peak_at_min > 0 else None
    return {"max_drawdown_ars": max_dd_ars, "max_drawdown_pct": max_dd_pct}


def compute_summary(
    closed_trades: List[ClosedTrade], open_positions_marked: pd.DataFrame,
) -> Dict[str, Any]:
    n = len(closed_trades)
    total_realized = sum(t.pnl_ars for t in closed_trades)
    total_unrealized = float(open_positions_marked["pnl_ars"].sum()) if not open_positions_marked.empty else 0.0

    wins = [t for t in closed_trades if t.pnl_ars > 0]
    losses = [t for t in closed_trades if t.pnl_ars < 0]
    win_rate = (len(wins) / n * 100.0) if n else None

    gross_profit = sum(t.pnl_ars for t in wins)
    gross_loss = abs(sum(t.pnl_ars for t in losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = None

    returns_pct = [t.pnl_pct for t in closed_trades]
    sharpe_approx = None
    if len(returns_pct) >= 2:
        mean_r = float(np.mean(returns_pct))
        std_r = float(np.std(returns_pct, ddof=1))
        sharpe_approx = (mean_r / std_r) if std_r > 1e-9 else None

    equity_curve = compute_equity_curve(closed_trades)
    drawdown = compute_max_drawdown(equity_curve)

    return {
        "n_closed_trades": n,
        "n_open_positions": int(len(open_positions_marked)) if not open_positions_marked.empty else 0,
        "pnl_realized_ars": total_realized,
        "pnl_unrealized_ars": total_unrealized,
        "pnl_total_ars": total_realized + total_unrealized,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "sharpe_approx": sharpe_approx,
        "max_drawdown_ars": drawdown["max_drawdown_ars"],
        "max_drawdown_pct": drawdown["max_drawdown_pct"],
    }


def closed_trades_to_frame(closed_trades: List[ClosedTrade]) -> pd.DataFrame:
    columns = [
        "symbol", "strategy", "direction", "quantity", "entry_time", "exit_time",
        "entry_price", "exit_price", "entry_order_id", "exit_order_id",
        "pnl_ars", "pnl_pct", "holding_seconds",
    ]
    if not closed_trades:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame([t.__dict__ for t in closed_trades], columns=columns)


# ---------------------------------------------------------------------------
# Smile de IV: puntos crudos (snapshot) + curva teorica (ajuste cuadratico)
# ---------------------------------------------------------------------------

def option_chain_snapshot_to_frame(bot_state: Dict[str, Any]) -> pd.DataFrame:
    columns = ["symbol", "strike", "expiry", "option_type", "bid", "ask", "mid", "iv", "spot_ref"]
    rows = bot_state.get("option_chain_snapshot", []) or []
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows)
    return df[[c for c in columns if c in df.columns]]


def fit_smile_curve(quotes_for_expiry: pd.DataFrame, n_points: int = 60) -> pd.DataFrame:
    """
    Replica liviana del ajuste que usa ggal_bot.models.volatility_surface.
    VolatilitySurface (cuadratico en log-moneyness) para poder dibujar una
    curva "teorica" suave, separada de los puntos crudos de IV por strike.
    Se recalcula aca (en vez de leer los coeficientes del bot) para no
    tener que tocar el ciclo de calculo de run_bot.py solo por esta
    visualizacion; el resultado es equivalente porque usa la misma forma
    funcional (cuadratica) sobre los mismos datos de IV cruda.
    """
    valid = quotes_for_expiry.dropna(subset=["iv", "spot_ref", "strike"])
    valid = valid[valid["spot_ref"] > 0]
    if len(valid) < 3:
        return pd.DataFrame(columns=["strike", "log_moneyness", "fitted_iv"])

    x = np.log(valid["strike"].astype(float) / valid["spot_ref"].astype(float))
    y = valid["iv"].astype(float)
    coeffs = np.polyfit(x, y, deg=2)  # [a, b, c] para a*x^2 + b*x + c

    spot_ref = float(valid["spot_ref"].iloc[0])
    x_grid = np.linspace(x.min(), x.max(), n_points)
    fitted = np.polyval(coeffs, x_grid)
    strikes_grid = spot_ref * np.exp(x_grid)
    return pd.DataFrame({"strike": strikes_grid, "log_moneyness": x_grid, "fitted_iv": fitted})
