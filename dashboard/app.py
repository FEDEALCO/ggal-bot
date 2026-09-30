"""
app.py (dashboard)
====================
Dashboard web local (Streamlit) para monitorear en tiempo real e
historicamente las operaciones del bot de opciones sobre GGAL: PnL por
trade y de portafolio, griegas agregadas, curva de equity, smile de IV con
los puntos donde el bot operó, y distribucion de retornos.

Consume:
    - logs/shadow_trades.csv   (ver ggal_bot.execution.order_gateway.ShadowAuditLogger)
    - state/bot_state.json     (ver ggal_bot.state_writer.StateWriter, extendido
                                 con option_chain_snapshot en run_bot.py)

Ninguno de los dos hace falta que existan para poder abrir el dashboard: si
el bot todavia no corrio, se muestra un estado vacio con instrucciones.

Uso:
    streamlit run dashboard/app.py
    (o doble click en run_dashboard.bat, ver ese archivo)
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Permite correr "streamlit run dashboard/app.py" desde la raiz del
# proyecto sin instalar el paquete: agrega la raiz a sys.path para poder
# importar ggal_bot.* y dashboard.pnl_engine.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from ggal_bot.config import SETTINGS  # noqa: E402
from ggal_bot.paths import POSITION_EVENTS_LOG, SHADOW_TRADES_LOG, STATE_FILE  # noqa: E402
from ggal_bot.risk.kill_switch import KillSwitch  # noqa: E402
from dashboard import pnl_engine as pe  # noqa: E402
from dashboard.data import journal as dj  # noqa: E402
from dashboard.data import reconciliation as rc  # noqa: E402

st.set_page_config(page_title="GGAL BOT — Dashboard", layout="wide", page_icon="📈")


# ---------------------------------------------------------------------------
# Sidebar: filtros + control de auto-refresh
# ---------------------------------------------------------------------------

st.sidebar.title("GGAL BOT")
st.sidebar.caption("Panel de monitoreo de PnL y griegas")

auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)
refresh_seconds = st.sidebar.slider("Intervalo (segundos)", min_value=2, max_value=30, value=5, disabled=not auto_refresh)

st.sidebar.divider()
st.sidebar.subheader("Filtros")

strategy_filter = st.sidebar.multiselect(
    "Estrategia", options=["weekly_asymmetric", "scalping", "vol_arbitrage", "delta_hedge", "unknown_legacy"],
    default=["weekly_asymmetric", "scalping", "vol_arbitrage", "delta_hedge", "unknown_legacy"],
    help=(
        "delta_hedge = rebalanceo del subyacente/futuro. Las demas se resuelven cruzando cada "
        "fill contra el Position Lifecycle Event Journal por client_order_id (ver "
        "pnl_engine.classify_strategy_from_journal) - 'unknown_legacy' = opcion sin match en ese "
        "journal (tipicamente anterior a 2026-09-07 17:05 UTC, cuando se desplego el journal): "
        "NO se adivina una estrategia para esas filas, ver REPORT.md SS12.0."
    ),
)
option_type_filter = st.sidebar.multiselect(
    "Tipo", options=["call", "put", "subyacente", "otro"],
    default=["call", "put", "subyacente", "otro"],
)
status_filter = st.sidebar.multiselect(
    "Estado", options=["Cerrada", "Abierta"], default=["Cerrada", "Abierta"],
)
symbol_search = st.sidebar.text_input("Buscar simbolo (contiene)", value="")

st.sidebar.divider()
st.sidebar.caption(
    "Fuente de datos:\n\n"
    f"- `{SHADOW_TRADES_LOG.name}`\n"
    f"- `{STATE_FILE.name}`\n\n"
    "Ver dashboard/pnl_engine.py para el detalle y las limitaciones del calculo de PnL."
)


# ---------------------------------------------------------------------------
# Carga de datos
# ---------------------------------------------------------------------------

fills = pe.load_fills()
bot_state = pe.load_bot_state()

if fills.empty:
    st.title("📈 GGAL BOT — Dashboard")
    st.info(
        "Todavia no hay operaciones registradas en "
        f"`{SHADOW_TRADES_LOG}`.\n\n"
        "Corre el bot (`python run_bot.py` o `run_bot.bat`) con "
        "`GGAL_BOT_SHADOW_MODE=true` para generar fills simulados, o esperá "
        "a que el bot en modo real registre operaciones."
    )
    st.stop()

position_events_df_raw = pe.load_position_events()

fills = fills.copy()
# CORREGIDO 2026-09-29 (ver REPORT.md SS12.0 y pnl_engine.py, docstring
# punto 2): classify_strategy(symbol) etiquetaba TODA opcion como
# "vol_arbitrage" sin importar que estrategia la abrio realmente.
# classify_strategy_from_journal() cruza cada fill contra el event journal
# por client_order_id (id exacto, no una aproximacion) y devuelve
# "unknown_legacy" en vez de adivinar cuando no hay match.
fills["strategy"] = pe.classify_strategy_from_journal(fills, position_events_df_raw)
# CORREGIDO 2026-09-30 (medido y verificado contra export-lifecycle.csv - ver
# docstring de match_trades_fifo para el bug real y la cifra exacta cruzada:
# ARS 1.361,50 en un solo simbolo): match_trades_fifo() agrupaba sus lotes
# FIFO SOLO por simbolo, lo que podia matchear el cierre de una estrategia
# contra un lote abierto de OTRA estrategia sobre el mismo simbolo. Esta
# columna cruza cada fill contra el event journal por client_order_id (mismo
# cruce exacto que classify_strategy_from_journal) para darle a
# match_trades_fifo el Position ID real cuando existe - "" si no hay match,
# en cuyo caso esa funcion cae a agrupar por simbolo+estrategia.
fills["position_id"] = pe.resolve_position_ids_from_journal(fills, position_events_df_raw)
fills["option_type"] = fills["symbol"].apply(pe.classify_option_type)

closed_trades, open_lots = pe.match_trades_fifo(fills)
open_positions_df = pe.aggregate_open_positions(open_lots)
open_positions_marked = pe.mark_to_market(open_positions_df, bot_state)
if not open_positions_marked.empty:
    open_positions_marked["option_type"] = open_positions_marked["symbol"].apply(pe.classify_option_type)

closed_df = pe.closed_trades_to_frame(closed_trades)
summary = pe.compute_summary(closed_trades, open_positions_marked)
equity_curve = pe.compute_equity_curve(closed_trades)


def _apply_filters(df: pd.DataFrame, symbol_col: str = "symbol") -> pd.DataFrame:
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if strategy_filter and "strategy" in df.columns:
        mask &= df["strategy"].isin(strategy_filter)
    if option_type_filter and "option_type" in df.columns:
        mask &= df["option_type"].isin(option_type_filter)
    if symbol_search:
        mask &= df[symbol_col].str.contains(symbol_search, case=False, na=False)
    return df[mask]


closed_df_f = _apply_filters(closed_df)
open_positions_f = _apply_filters(open_positions_marked)


# ---------------------------------------------------------------------------
# Header + KPI cards
# ---------------------------------------------------------------------------

st.title("📈 GGAL BOT — Dashboard de Trading")
last_update = bot_state.get("timestamp")
st.caption(
    f"Ultima actualizacion del bot: {last_update or 'sin datos de state/bot_state.json todavia'} · "
    f"Ahora: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
)

kpi_row1 = st.columns(4)
kpi_row1[0].metric(
    "PnL Total (ARS)",
    f"$ {summary['pnl_total_ars']:,.2f}",
    delta=f"Realizado $ {summary['pnl_realized_ars']:,.2f} · No realizado $ {summary['pnl_unrealized_ars']:,.2f}",
)
kpi_row1[1].metric(
    "Win Rate",
    f"{summary['win_rate_pct']:.1f}%" if summary["win_rate_pct"] is not None else "—",
    delta=f"{summary['n_closed_trades']} trades cerrados",
)
kpi_row1[2].metric(
    "Profit Factor",
    (f"{summary['profit_factor']:.2f}" if isinstance(summary["profit_factor"], float) and summary["profit_factor"] != float("inf") else ("∞" if summary["profit_factor"] == float("inf") else "—")),
)
kpi_row1[3].metric(
    "Max Drawdown",
    f"$ {summary['max_drawdown_ars']:,.2f}",
    delta=(f"{summary['max_drawdown_pct']:.1f}%" if summary["max_drawdown_pct"] is not None else "sin pico positivo aun"),
    delta_color="inverse",
)

totals = bot_state.get("portfolio_greeks_total", {}) or {}
kpi_row2 = st.columns(5)
kpi_row2[0].metric("Delta (Δ)", f"{totals.get('delta', 0.0):,.1f}")
kpi_row2[1].metric("Gamma (Γ)", f"{totals.get('gamma', 0.0):,.4f}")
kpi_row2[2].metric("Vega (V)", f"$ {totals.get('vega', 0.0):,.1f} / vol pt")
kpi_row2[3].metric("Theta (Θ)", f"$ {totals.get('theta', 0.0):,.1f} / dia")
kpi_row2[4].metric(
    "Sharpe (aprox., sin anualizar)",
    f"{summary['sharpe_approx']:.2f}" if summary["sharpe_approx"] is not None else "—",
)

if bot_state.get("risk_breaches") and "LIMITE EXCEDIDO" in str(bot_state.get("risk_breaches")):
    st.warning(f"⚠️ {bot_state['risk_breaches']}")

# Kill switch centralizado (Fase 5.3, ver ggal_bot/risk/kill_switch.py):
# se relee de disco en cada refresh del dashboard, asi que un trip()
# disparado por el bot en su propio proceso se ve aca sin reiniciar nada.
_ks_state = KillSwitch().status()
if _ks_state.tripped:
    st.error(
        f"🛑 KILL SWITCH DISPARADO ({_ks_state.tripped_by}, {_ks_state.tripped_at}): "
        f"{_ks_state.reason}\n\n"
        "El bot NO abrira posiciones nuevas hasta un reset manual "
        "(`python -m ggal_bot.risk.kill_switch --reset \"motivo\"`). Las salidas de "
        "posiciones ya abiertas NO estan bloqueadas por este mecanismo."
    )

st.divider()


# ---------------------------------------------------------------------------
# Panel de reconciliacion (Fase 1, mandato explicito del usuario - ver
# REPORT.md: "PnL total del dashboard = suma de PnL por estrategia = PnL
# reconstruido desde el journal, sin duplicados. Si no cuadra, banner rojo
# con la diferencia.")
# ---------------------------------------------------------------------------

st.subheader("🔍 Reconciliación")

_JOURNAL_STRATEGIES = ("weekly_asymmetric", "scalping", "vol_arbitrage")
_RECON_TOLERANCE_ARS = 1.0  # redondeo de punto flotante, no una discrepancia real

# IMPORTANTE: reusa position_events_df_raw (ya cargado mas arriba, tambien
# usado para classify_strategy_from_journal() y la pestaña "Lifecycle") -
# no se vuelve a leer logs/position_events.csv del disco.
journal_rows_all = dj.rows_from_position_events_df(position_events_df_raw)

# --- Chequeo 1: la suma de PnL cerrado por estrategia (sobre el universo
# COMPLETO, closed_df sin filtrar por la sidebar - la reconciliacion no
# puede depender de lo que el usuario eligio mirar) tiene que coincidir con
# el PnL realizado total de la KPI de arriba. ---
if closed_df.empty:
    fifo_sum_by_strategy: Dict[str, float] = {}
else:
    fifo_sum_by_strategy = closed_df.groupby("strategy")["pnl_ars"].sum().to_dict()

fifo_total_check = sum(fifo_sum_by_strategy.values())
kpi_total = summary["pnl_realized_ars"]
sum_matches_kpi = abs(fifo_total_check - kpi_total) <= _RECON_TOLERANCE_ARS

# --- Chequeo 2: reconstruccion INDEPENDIENTE desde position_events.csv vs
# el total FIFO de la MISMA estrategia. Dos fuentes de datos y dos caminos
# de codigo distintos que deben coincidir (con tolerancia de redondeo) -
# exactamente la clase de bug que motivo este proyecto entero (ver
# classify_strategy()).
#
# MEJORA 2026-09-30 (Prioridad 2 a pedido explicito del usuario - "journal
# para todas las estrategias, sin excepciones"): vol_arbitrage
# (_act_on_signal en run_bot.py) ya loguea su ENTRY al journal (antes solo
# tocaba self.portfolio directo) - se suma aca porque su lifecycle es
# comparable al de weekly_asymmetric/scalping (abre y eventualmente CIERRA
# via _check_vol_arbitrage_exits, que ya logueaba el CLOSE desde antes).
#
# delta_hedge TAMBIEN loguea ya su lifecycle completo (ENTRY/ADD/REDUCE/
# CLOSE, ver _maybe_hedge) pero DELIBERADAMENTE NO se agrega aca todavia:
# verificado (ver reconstruct.reconstruct_lifecycle_trades) que una
# posicion sin evento CLOSE dentro de la ventana se cuenta 100% como
# "todavia abierta" y su PnL de eventuales REDUCE NUNCA se suma a
# journal_pnl - y la Position de delta_hedge es UNA sola, continuamente
# reajustada (ver _maybe_hedge), que en operacion normal casi nunca llega a
# CLOSE (cantidad exactamente 0). Agregarla aca produciria un ❌ de
# reconciliacion PERMANENTE y enganoso (PnL FIFO real vs. journal_pnl=0 por
# diseño de reconstruct_lifecycle_trades), no un bug real - se documenta
# como limitacion conocida en el caption de abajo en vez de fabricar una
# comparacion que no es honesta con los datos disponibles.
# `unknown_legacy` sigue sin cobertura aca: por definicion, es un fill SIN
# match en el journal (anterior a su deploy, 2026-09-07 17:05 UTC) - no hay
# nada que reconstruir.
recon_table_rows = []
any_journal_mismatch = False
partition_issues = []

for strat in _JOURNAL_STRATEGIES:
    fifo_pnl = float(fifo_sum_by_strategy.get(strat, 0.0))
    fifo_n = int((closed_df["strategy"] == strat).sum()) if not closed_df.empty else 0

    rows_for_strat = [r for r in journal_rows_all if r.get("strategy_tag") == strat]
    closed_result = dj.get_closed_trades(journal_rows_all, strategies=(strat,))
    open_result = dj.get_open_positions(journal_rows_all, strategies=(strat,))
    trades, _, _ = closed_result
    journal_pnl = sum(t.pnl_gross_ars for t in trades)
    journal_n = len(trades)

    diff = fifo_pnl - journal_pnl
    has_any_data = (fifo_n > 0) or (journal_n > 0)
    matches = (not has_any_data) or (abs(diff) <= max(_RECON_TOLERANCE_ARS, abs(journal_pnl) * 0.01))
    if has_any_data and not matches:
        any_journal_mismatch = True

    partition_check = rc.cross_check_partition(rows_for_strat, closed_result, open_result)
    if not partition_check.is_consistent:
        partition_issues.append((strat, partition_check.detail))

    recon_table_rows.append({
        "Estrategia": strat,
        "PnL FIFO — shadow_trades.csv (bruto)": fifo_pnl,
        "N (FIFO)": fifo_n,
        "PnL Journal — position_events.csv (bruto)": journal_pnl,
        "N (Journal)": journal_n,
        "Diferencia": diff,
        "¿Coincide?": "✅" if matches else ("— sin datos" if not has_any_data else "❌"),
    })

recon_df = pd.DataFrame(recon_table_rows)
if not recon_df.empty:
    for _col in ["PnL FIFO — shadow_trades.csv (bruto)", "PnL Journal — position_events.csv (bruto)", "Diferencia"]:
        recon_df[_col] = recon_df[_col].round(2)
    st.dataframe(recon_df, width="stretch", hide_index=True)

if not sum_matches_kpi:
    st.error(
        f"🔴 RECONCILIACIÓN FALLIDA: la suma de PnL cerrado por estrategia (\\$ {fifo_total_check:,.2f}) "
        f"no coincide con el PnL realizado total de la KPI (\\$ {kpi_total:,.2f}) — diferencia "
        f"\\$ {fifo_total_check - kpi_total:,.2f}. Esto indicaria un bug de agregacion interno; "
        "no deberia pasar nunca (ambos numeros salen de la misma lista de trades cerrados)."
    )
elif any_journal_mismatch:
    st.error(
        "🔴 RECONCILIACIÓN FALLIDA: el PnL reconstruido de forma independiente desde el Event Journal "
        "no coincide con el PnL FIFO (shadow_trades.csv) para al menos una estrategia — ver la "
        "diferencia en la tabla de arriba. `match_trades_fifo()` ya agrupa lotes abiertos por Position "
        "ID cuando hay match en el journal (o por simbolo+estrategia si no lo hay — corregido "
        "2026-09-30, ver docstring de esa funcion), asi que un cruce entre estrategias del mismo "
        "simbolo ya NO deberia ser la causa. Motivos probables a investigar: fills sin match en el "
        "journal (anteriores a 2026-09-07 17:05 UTC, cuando se desplego el journal, o de una estrategia "
        "que todavia no loguea sus ENTRY al journal — vol_arbitrage/delta_hedge) o posiciones legacy "
        "con datos incompletos."
    )
else:
    st.success(
        "✅ Reconciliación OK: sin duplicados detectados, y el PnL FIFO coincide con el PnL "
        "reconstruido de forma independiente desde el Event Journal (dentro de tolerancia)."
    )

if partition_issues:
    for _strat, _detail in partition_issues:
        st.warning(f"⚠️ Inconsistencia de partición en el journal para '{_strat}': {_detail}")

st.caption(
    "Reconciliación de solo lectura: no corrige nada, solo compara dos caminos de calculo "
    "independientes (FIFO sobre `shadow_trades.csv` vs. reconstruccion desde `position_events.csv`). "
    "Desde el 2026-09-30 las 4 estrategias (`weekly_asymmetric`, `scalping`, `vol_arbitrage`, "
    "`delta_hedge`) loguean su lifecycle completo al Event Journal, pero la tabla de arriba solo "
    "cruza `weekly_asymmetric`/`scalping`/`vol_arbitrage`: `delta_hedge` es UNA sola posicion "
    "continuamente reajustada que casi nunca llega a un evento CLOSE exacto, y la reconstruccion "
    "del journal (`reconstruct_lifecycle_trades`) solo cuenta PnL de una posicion que SI cerro — "
    "cruzarla aca daria un ❌ permanente y enganoso, no un bug real. `unknown_legacy` tampoco tiene "
    "equivalente por definicion (fill sin match en el journal, anterior a su deploy el 2026-09-07 "
    "17:05 UTC): para esas dos, la unica fuente disponible es el FIFO, sin verificacion cruzada."
)

st.divider()


# ---------------------------------------------------------------------------
# Tabla de operaciones
# ---------------------------------------------------------------------------

st.subheader("Operaciones")


def _position_events_for_display(events_df: pd.DataFrame) -> pd.DataFrame:
    """
    Reordena logs/position_events.csv (ya cargado una sola vez arriba via
    pe.load_position_events(), reusado tambien para
    classify_strategy_from_journal() - no se relee el archivo dos veces)
    para la pestaña "Lifecycle": mas reciente primero.
    """
    if events_df.empty:
        return events_df
    return events_df.sort_values("timestamp_utc", ascending=False).reset_index(drop=True)


position_events_df = _position_events_for_display(position_events_df_raw)

tab_closed, tab_open, tab_lifecycle = st.tabs([
    f"Cerradas ({len(closed_df_f)})", f"Abiertas ({len(open_positions_f)})",
    f"Lifecycle ({len(position_events_df)})",
])

with tab_closed:
    if "Cerrada" not in status_filter:
        st.caption("Filtro de Estado no incluye 'Cerrada'.")
    elif closed_df_f.empty:
        st.caption("No hay trades cerrados que coincidan con los filtros.")
    else:
        display = closed_df_f.copy()
        display["entry_time"] = display["entry_time"].dt.strftime("%Y-%m-%d %H:%M:%S")
        display["exit_time"] = display["exit_time"].dt.strftime("%Y-%m-%d %H:%M:%S")
        display["pnl_ars"] = display["pnl_ars"].round(2)
        display["pnl_pct"] = display["pnl_pct"].round(2)
        display["holding_seconds"] = display["holding_seconds"].round(0)
        st.dataframe(
            display[[
                "symbol", "strategy", "direction", "quantity", "entry_time", "exit_time",
                "entry_price", "exit_price", "pnl_ars", "pnl_pct", "holding_seconds",
            ]].rename(columns={
                "symbol": "Ticker", "strategy": "Estrategia", "direction": "Direccion",
                "quantity": "Cantidad", "entry_time": "Entrada", "exit_time": "Salida",
                "entry_price": "Precio Entrada", "exit_price": "Precio Salida",
                "pnl_ars": "PnL ($)", "pnl_pct": "PnL (%)", "holding_seconds": "Duracion (s)",
            }),
            width="stretch", hide_index=True,
        )

with tab_open:
    if "Abierta" not in status_filter:
        st.caption("Filtro de Estado no incluye 'Abierta'.")
    elif open_positions_f.empty:
        st.caption("No hay posiciones abiertas que coincidan con los filtros.")
    else:
        display = open_positions_f.copy()
        display["entry_time"] = display["entry_time"].dt.strftime("%Y-%m-%d %H:%M:%S")
        display["avg_entry_price"] = display["avg_entry_price"].round(4)
        display["current_price"] = display["current_price"].round(4)
        display["pnl_ars"] = display["pnl_ars"].round(2)
        display["pnl_pct"] = display["pnl_pct"].round(2)
        display["Estado"] = display["has_current_price"].map(
            {True: "Abierta", False: "Abierta (sin cotizacion actual)"}
        )
        st.dataframe(
            display[[
                "symbol", "strategy", "quantity", "entry_time", "avg_entry_price",
                "current_price", "pnl_ars", "pnl_pct", "Estado",
            ]].rename(columns={
                "symbol": "Ticker", "strategy": "Estrategia", "quantity": "Cantidad",
                "entry_time": "Entrada", "avg_entry_price": "Precio Entrada (prom.)",
                "current_price": "Precio Actual", "pnl_ars": "PnL no realizado ($)",
                "pnl_pct": "PnL no realizado (%)",
            }),
            width="stretch", hide_index=True,
        )
        st.caption(
            "'Sin cotizacion actual' = la base ya no aparece en la cadena vigente del bot "
            "(vencio o rodo fuera del universo de vencimientos configurado); el PnL no realizado "
            "de esas filas queda en $0 hasta que se resuelva manualmente."
        )

with tab_lifecycle:
    st.caption(
        "Position Lifecycle Event Journal (Fase 5.3) - un evento por cada ENTRY/REDUCE/"
        "PARTIAL_EXIT/CLOSE/REJECT real, con position_id/contract_key/strategy_tag. "
        "Archivo NUEVO e independiente de shadow_trades.csv: solo tiene datos a partir de "
        "este deploy hacia adelante, no reconstruye el historial previo."
    )
    if position_events_df.empty:
        st.info(
            f"Todavia no hay eventos en `{POSITION_EVENTS_LOG}` (vacio hasta el primer "
            "ENTRY/REDUCE/CLOSE/REJECT que ocurra con el codigo de esta fase ya desplegado)."
        )
    else:
        event_filter = st.multiselect(
            "Tipo de evento", options=sorted(position_events_df["event_type"].dropna().unique()),
            default=list(sorted(position_events_df["event_type"].dropna().unique())),
        )
        df_display = position_events_df[position_events_df["event_type"].isin(event_filter)].copy()
        df_display["timestamp_utc_str"] = df_display["timestamp_utc"].dt.strftime("%Y-%m-%d %H:%M:%S")
        st.dataframe(
            df_display[[
                "timestamp_utc_str", "event_type", "symbol", "strategy_tag", "position_id",
                "contract_key", "side", "quantity_delta", "quantity_after", "price",
                "reason", "data_unavailable_fields",
            ]].rename(columns={
                "timestamp_utc_str": "Cuando (UTC)", "event_type": "Evento", "symbol": "Ticker",
                "strategy_tag": "Estrategia", "position_id": "Position ID",
                "contract_key": "Contract Key", "side": "Lado", "quantity_delta": "Δ Cantidad",
                "quantity_after": "Cantidad restante", "price": "Precio", "reason": "Motivo",
                "data_unavailable_fields": "Campos no disponibles",
            }),
            width="stretch", hide_index=True,
        )

        st.caption(
            "Resumen por Position ID (agrupa todos los eventos de UN mismo lote, no de un "
            "mismo simbolo - dos lotes distintos del mismo ticker tienen Position ID distinto):"
        )
        # BUG REAL EVITADO EN REVISION (ver smoke test de esta fase): df_display
        # esta ordenado DESCENDENTE por timestamp (mas reciente primero, para
        # la tabla de arriba) - agrupar sobre ESE orden y pedir agg(...,"last")
        # devuelve la fila mas VIEJA de cada grupo, no la mas reciente. Se
        # ordena ASCENDENTE explicitamente aca, en una copia separada, solo
        # para este resumen, para que "last" signifique lo que dice.
        df_for_summary = df_display.sort_values("timestamp_utc", ascending=True)
        summary_by_pos = (
            df_for_summary[df_for_summary["position_id"].astype(bool)]
            .groupby("position_id")
            .agg(
                symbol=("symbol", "first"),
                strategy_tag=("strategy_tag", "first"),
                n_eventos=("event_type", "count"),
                primer_evento=("timestamp_utc", "min"),
                ultimo_evento=("timestamp_utc", "max"),
                ultima_cantidad=("quantity_after", "last"),
            )
            .reset_index()
            .sort_values("ultimo_evento", ascending=False)
        )
        if not summary_by_pos.empty:
            summary_by_pos["primer_evento"] = summary_by_pos["primer_evento"].dt.strftime("%Y-%m-%d %H:%M:%S")
            summary_by_pos["ultimo_evento"] = summary_by_pos["ultimo_evento"].dt.strftime("%Y-%m-%d %H:%M:%S")
            st.dataframe(
                summary_by_pos.rename(columns={
                    "position_id": "Position ID", "symbol": "Ticker", "strategy_tag": "Estrategia",
                    "n_eventos": "N° eventos", "primer_evento": "Primer evento",
                    "ultimo_evento": "Ultimo evento", "ultima_cantidad": "Cantidad actual",
                }),
                width="stretch", hide_index=True,
            )

st.divider()


# ---------------------------------------------------------------------------
# Graficos
# ---------------------------------------------------------------------------

col_equity, col_hist = st.columns([2, 1])

with col_equity:
    st.subheader("Curva de Equity (PnL realizado acumulado)")
    if equity_curve.empty:
        st.caption("Sin trades cerrados todavia.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=equity_curve["timestamp"], y=equity_curve["cumulative_pnl_ars"],
            mode="lines+markers", name="Equity acumulado", line=dict(width=2),
        ))
        fig.update_layout(
            xaxis_title="Fecha/hora", yaxis_title="PnL acumulado (ARS)",
            margin=dict(l=10, r=10, t=10, b=10), height=350,
        )
        st.plotly_chart(fig, width="stretch")

with col_hist:
    st.subheader("Distribucion de retornos")
    if closed_df.empty:
        st.caption("Sin trades cerrados todavia.")
    else:
        fig = px.histogram(closed_df, x="pnl_ars", nbins=30, labels={"pnl_ars": "PnL por trade (ARS)"})
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=350, yaxis_title="Cantidad de trades")
        st.plotly_chart(fig, width="stretch")

st.subheader("Smile de Volatilidad Implícita")
snapshot_df = pe.option_chain_snapshot_to_frame(bot_state)
if snapshot_df.empty:
    st.caption(
        "Sin datos de la cadena de opciones todavia en `state/bot_state.json` "
        "(esperando a que el bot corra al menos un ciclo)."
    )
else:
    traded_symbols = set(fills["symbol"].unique())
    expiries = sorted(snapshot_df["expiry"].dropna().unique())
    expiry_choice = st.selectbox("Vencimiento", options=expiries) if expiries else None

    if expiry_choice is not None:
        quotes_for_expiry = snapshot_df[snapshot_df["expiry"] == expiry_choice].copy()
        smile_curve = pe.fit_smile_curve(quotes_for_expiry)

        quotes_for_expiry["fue_operada"] = quotes_for_expiry["symbol"].isin(traded_symbols)

        fig = go.Figure()
        if not smile_curve.empty:
            fig.add_trace(go.Scatter(
                x=smile_curve["strike"], y=smile_curve["fitted_iv"] * 100.0,
                mode="lines", name="Curva teorica (ajuste cuadratico)",
                line=dict(color="rgba(120,120,220,0.9)", width=2),
            ))

        not_traded = quotes_for_expiry[~quotes_for_expiry["fue_operada"]]
        traded = quotes_for_expiry[quotes_for_expiry["fue_operada"]]

        fig.add_trace(go.Scatter(
            x=not_traded["strike"], y=not_traded["iv"] * 100.0, mode="markers",
            name="IV cruda (no operada)", marker=dict(size=7, color="rgba(150,150,150,0.7)"),
        ))
        fig.add_trace(go.Scatter(
            x=traded["strike"], y=traded["iv"] * 100.0, mode="markers+text",
            name="Operada por el bot", text=traded["symbol"], textposition="top center",
            marker=dict(size=11, color="rgba(220,80,80,0.95)", symbol="diamond", line=dict(width=1, color="white")),
        ))
        fig.update_layout(
            xaxis_title="Strike", yaxis_title="IV (%)",
            margin=dict(l=10, r=10, t=10, b=10), height=420,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "La curva teorica es un ajuste cuadratico en log-moneyness sobre los puntos crudos de "
            "este snapshot (misma forma funcional que ggal_bot.models.volatility_surface, recalculada "
            "aca para no acoplar el dashboard al ciclo de trading). Los diamantes rojos marcan bases "
            "sobre las que el bot ya opero (en cualquier momento, no necesariamente en este snapshot)."
        )

st.divider()
st.caption(
    "Este dashboard consolida el PnL de las ordenes que genero el bot (ver "
    "dashboard/pnl_engine.py, docstring, para el alcance y las limitaciones). "
    "No reemplaza una conciliacion contra el estado de cuenta real del ALYC."
)


# ---------------------------------------------------------------------------
# Auto-refresh: relee los archivos y vuelve a dibujar toda la pagina.
# ---------------------------------------------------------------------------

if auto_refresh:
    time.sleep(refresh_seconds)
    st.rerun()
