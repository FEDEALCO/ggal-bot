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
    "Estrategia", options=["vol_arbitrage", "delta_hedge"],
    default=["vol_arbitrage", "delta_hedge"],
    help="vol_arbitrage = señales de smile (opciones); delta_hedge = rebalanceo del subyacente/futuro.",
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

fills = fills.copy()
fills["strategy"] = fills["symbol"].apply(pe.classify_strategy)
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
# Tabla de operaciones
# ---------------------------------------------------------------------------

st.subheader("Operaciones")


def _load_position_events() -> pd.DataFrame:
    """
    Lee logs/position_events.csv (Fase 5.3, ver
    ggal_bot/portfolio/event_journal.py) - archivo NUEVO e independiente de
    shadow_trades.csv, arranca vacio/inexistente hasta el primer evento de
    lifecycle registrado DESPUES de este deploy (no reconstruye historial
    previo, ver docstring del modulo).
    """
    if not POSITION_EVENTS_LOG.exists() or POSITION_EVENTS_LOG.stat().st_size == 0:
        return pd.DataFrame()
    try:
        df = pd.read_csv(POSITION_EVENTS_LOG)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame()
    if df.empty:
        return df
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    return df.sort_values("timestamp_utc", ascending=False).reset_index(drop=True)


position_events_df = _load_position_events()

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
