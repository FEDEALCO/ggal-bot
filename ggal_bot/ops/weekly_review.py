"""
weekly_review.py
=================
Revision semanal en UN COMANDO (a pedido explicito del usuario, 2026-10-15,
punto 4, dentro del congelamiento de 3 semanas de FREEZE.md - esto es
"analisis offline", explicitamente permitido durante esa ventana). Pensado
para correr cada viernes despues de las 17:00, junta desde los logs reales
de produccion las 6 cosas que el usuario pidio textualmente:

    1) Trades y PnL neto por estrategia, con intervalo de confianza
       (bootstrap) - reutiliza ggal_bot/backtest/metrics.py (bootstrap_ci,
       build_strategy_report), la MISMA logica ya usada y testeada en
       Fase 0 (ver ggal_bot/backtest/run_fase0.py), aplicada aca sobre
       logs/position_events.csv en vez de un export puntual.
    2) Violaciones de invariantes - reutiliza audit_journal_invariants.py
       (raiz del repo, ya existente, ver ALL_CHECKS) sobre
       logs/position_events.csv.
    3) Fills fuera de horario de rueda - reutiliza
       dashboard/data/market_hours_quality.py (flag_fills_outside_session)
       sobre logs/shadow_trades.csv (+ siblings rotados por cambio de
       schema, via dashboard/pnl_engine.py::load_fills).
    4) % de quotes validas por contrato REALMENTE OPERADO - cruza los
       simbolos que aparecen en logs/position_events.csv contra
       logs/market_snapshots.csv (+ siblings .gz rotados a diario), usando
       el mismo criterio de "quote operable" que el resto del proyecto
       (bid>0, ask>0, spread relativo <= RiskConfig.max_spread_relative).
    5) Disparos del respaldo de ultimo resorte (quote-gap backstop) -
       parsea logs/ggal_bot.log buscando las lineas "RESPALDO [...]
       disparado" que run_bot.py::_apply_quote_gap_backstop ya emite (no
       existe un CSV estructurado para esto - ver RiskManager.
       evaluate_quote_gap_backstop).
    6) Acumulacion de datos para Hipotesis 1 (RESEARCH.md): cuantas filas
       de logs/market_snapshots.csv (+ siblings) tienen last_price>0,
       desde que ese campo se empezo a loguear (ver RESEARCH.md, seccion
       "Hipotesis 1 - actualizacion 2026-10-14").

Uso (desde la RAIZ del repo, igual que run_fase0.py/weekend_guard_check.py -
sin esto, el import a nivel de modulo de `audit_journal_invariants`, que
vive en la raiz del repo y no dentro del paquete ggal_bot, no resuelve):

    python -m ggal_bot.ops.weekly_review
    python -m ggal_bot.ops.weekly_review --logs-dir /ruta/a/otro/logs
    python -m ggal_bot.ops.weekly_review --out informe_2026-10-17.md

Es SOLO LECTURA/DIAGNOSTICO (mismo criterio que
dashboard/data/market_hours_quality.py y audit_journal_invariants.py):
nunca escribe ni modifica ningun log de produccion. El unico archivo que
escribe es el reporte final (default: weekly_review_<fecha>.md en el
directorio donde se lo corre).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# `audit_journal_invariants.py` vive en la raiz del repo (no es parte del
# paquete ggal_bot - ver docstring de ese archivo). Se agrega la raiz a
# sys.path de forma defensiva por si este modulo se invoca desde otro cwd
# (ej. un cron job con working directory distinto) - `python -m` ya la
# agrega automaticamente cuando se corre desde la raiz, pero no cuesta
# nada ser explicito y no depender de eso silenciosamente.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import audit_journal_invariants as aji  # noqa: E402

from ggal_bot import paths  # noqa: E402
from ggal_bot.backtest.metrics import build_strategy_report  # noqa: E402
from ggal_bot.backtest.costs import CostAssumptions  # noqa: E402
from ggal_bot.backtest.reconstruct import reconstruct_lifecycle_trades  # noqa: E402
from ggal_bot.config import SETTINGS  # noqa: E402
from ggal_bot.market_hours import is_within_byma_session  # noqa: E402

logger = logging.getLogger("ggal_bot.ops.weekly_review")


# ---------------------------------------------------------------------------
# Carga de datos (todas tolerantes a archivo ausente/vacio - un log que
# todavia no existe es un caso legitimo, ver criterio de
# dashboard/pnl_engine.py::load_fills/load_position_events).
# ---------------------------------------------------------------------------

def load_position_events_rows(logs_dir: Path) -> List[Dict]:
    """
    Lee logs/position_events.csv directo (headers en INGLES, ya calzan
    1:1 con las claves internas que espera
    backtest.reconstruct.reconstruct_lifecycle_trades - a diferencia de
    load_lifecycle_journal_rows(), que traduce un export en ESPAÑOL del
    dashboard, este archivo es el journal real de produccion, sin
    traducir). Devuelve [] si el archivo no existe o esta vacio.
    """
    path = logs_dir / "position_events.csv"
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: r.get("timestamp_utc") or "")
    return rows


def _discover_rotated_family(live_path: Path, stem: str) -> List[Path]:
    """
    `live_path` + cualquier sibling `{stem}-YYYY-MM-DD{suffix}.gz` en el
    mismo directorio (ver market_snapshot_log.py/order_gateway.py: ambos
    rotan con este mismo patron de nombre). Orden cronologico (los
    nombres ya son lexicograficamente ordenables por fecha).
    """
    out = []
    if live_path.exists() and live_path.stat().st_size > 0:
        out.append(live_path)
    if live_path.parent.exists():
        pattern = re.compile(rf"^{re.escape(stem)}-\d{{4}}-\d{{2}}-\d{{2}}\.csv(\.gz)?$")
        for p in sorted(live_path.parent.iterdir()):
            if pattern.match(p.name) and p.stat().st_size > 0:
                out.append(p)
    return out


def load_market_snapshot_rows(logs_dir: Path) -> List[Dict]:
    """
    Lee logs/market_snapshots.csv (vivo) + todos los siblings rotados
    `-YYYY-MM-DD.csv.gz` (ver market_snapshot_log.py) disponibles en
    `logs_dir`. Cada fila es un dict con las columnas reales del header
    (ver MarketSnapshotLogger): timestamp_utc, symbol, option_type,
    strike, expiry, days_calendar, days_business, spot_ref, bid, ask,
    bid_size, ask_size, iv, delta, gamma, vega, theta, last_price,
    last_volume. [] si no hay ningun archivo con datos.
    """
    live = logs_dir / "market_snapshots.csv"
    files = _discover_rotated_family(live, "market_snapshots")
    rows: List[Dict] = []
    for path in files:
        opener = gzip.open if path.suffix == ".gz" else open
        mode = "rt"
        with opener(path, mode, encoding="utf-8", newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


def load_shadow_fill_rows(logs_dir: Path) -> List[Dict]:
    """
    Lee logs/shadow_trades.csv (vivo) + siblings rotados por cambio de
    schema (`.schemaN.csv`, ver ShadowAuditLogger) presentes en
    `logs_dir`, y devuelve solo las filas `event == "shadow_fill"`.
    Deliberadamente simple (no reusa dashboard/pnl_engine.py::load_fills
    para no traer pandas como dependencia dura de esta herramienta ni
    duplicar su logica de parseo de timestamps con microsegundos mixtos -
    ver docstring de esa funcion -; esta herramienta solo necesita el
    timestamp y el symbol para la guardia de horario, no los precios).
    """
    live = logs_dir / "shadow_trades.csv"
    files = []
    if live.exists() and live.stat().st_size > 0:
        files.append(live)
    if live.parent.exists():
        pattern = re.compile(r"^shadow_trades\.schema\d+\.csv$")
        for p in sorted(live.parent.iterdir()):
            if pattern.match(p.name) and p.stat().st_size > 0:
                files.append(p)
    rows: List[Dict] = []
    for path in files:
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("event") == "shadow_fill":
                    rows.append(row)
    return rows


def load_backstop_trigger_lines(logs_dir: Path) -> List[str]:
    """
    Parsea logs/ggal_bot.log (texto libre, sin rotacion conocida de este
    modulo - se lee tal cual esta) buscando las lineas que
    run_bot.py::_apply_quote_gap_backstop ya emite textualmente:
    "RESPALDO [<reason>] disparado para <symbol>...". No existe hoy un
    CSV estructurado para esto (ver RiskManager.evaluate_quote_gap_backstop) -
    si en el futuro se agrega uno, esta funcion deberia migrar a leerlo en
    vez de parsear texto. Devuelve la lista de lineas crudas que
    matchean (el parseo de reason/symbol se hace en el caller, para poder
    testear ambos pasos por separado).
    """
    path = logs_dir / "ggal_bot.log"
    if not path.exists() or path.stat().st_size == 0:
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "RESPALDO [" in line and "disparado" in line:
                out.append(line.rstrip("\n"))
    return out


_BACKSTOP_LINE_RE = re.compile(r"RESPALDO \[(?P<reason>[^\]]+)\] disparado para (?P<symbol>[^:\s]+)")


def parse_backstop_triggers(lines: List[str]) -> List[Tuple[str, str]]:
    """[(reason, symbol), ...] a partir de las lineas crudas de load_backstop_trigger_lines."""
    out = []
    for line in lines:
        m = _BACKSTOP_LINE_RE.search(line)
        if m:
            out.append((m.group("reason"), m.group("symbol")))
    return out


# ---------------------------------------------------------------------------
# Secciones del reporte
# ---------------------------------------------------------------------------

def section_trades_pnl_by_strategy(journal_rows: List[Dict]) -> str:
    """
    Trades cerrados y PnL neto por estrategia, con intervalo de confianza
    bootstrap (ver backtest/metrics.py::build_strategy_report/bootstrap_ci -
    misma logica que Fase 0, ningun calculo nuevo). Escenario de costo:
    CostAssumptions() por defecto (gold, spread_round_trip_pct=0.0) - el
    "caso base" ya usado como referencia en todo el proyecto; no se repite
    aca la grilla completa de sensibilidad de Fase 0 (eso sigue siendo
    responsabilidad de run_fase0.py bajo pedido puntual, no de una
    revision semanal).
    """
    if not journal_rows:
        return "DATA INSUFFICIENT - logs/position_events.csv no existe o esta vacio."

    strategy_tags = sorted({(r.get("strategy_tag") or "weekly_asymmetric") for r in journal_rows})
    scenario = CostAssumptions()
    lines = [
        f"{'estrategia':<22}{'n_cerrados':>11}{'abiertos':>10}{'win%':>8}{'net_pnl_ars':>16}"
        f"{'IC95% net_pnl*':>18}{'expectancy/tr':>15}{'IC95% expectancy':>20}",
    ]
    lines.append("-" * len(lines[0]))
    for tag in strategy_tags:
        trades, still_open, incomplete = reconstruct_lifecycle_trades(journal_rows, strategies=(tag,))
        report = build_strategy_report(tag, trades, scenario, "mid_sin_spread",
                                        n_still_open_excluded=still_open, n_excluded_incomplete_data=incomplete)
        exp_ci = report.expectancy_ci
        # El bootstrap de metrics.py da IC de EXPECTANCY (ars/trade), no
        # directo de net_pnl_ars total - se aproxima el IC de la suma
        # multiplicando los limites de expectancy por n (consistente con
        # net_pnl_ars = expectancy_ars_per_trade * n cuando n>0; si n==0 o
        # el bootstrap no pudo calcularse (None), se deja "n/d" en vez de
        # fabricar un numero).
        if exp_ci[0] is not None and exp_ci[1] is not None and report.n_trades > 0:
            net_ci_lo = exp_ci[0] * report.n_trades
            net_ci_hi = exp_ci[1] * report.n_trades
            net_ci_str = f"[{net_ci_lo:,.0f}, {net_ci_hi:,.0f}]"
        else:
            net_ci_str = "n/d"
        exp_ci_str = (
            f"[{exp_ci[0]:,.0f}, {exp_ci[1]:,.0f}]" if exp_ci[0] is not None and exp_ci[1] is not None else "n/d"
        )
        win_str = f"{report.win_rate_pct:.1f}" if report.win_rate_pct is not None else "n/d"
        exp_str = f"{report.expectancy_ars_per_trade:,.0f}" if report.expectancy_ars_per_trade is not None else "n/d"
        lines.append(
            f"{tag:<22}{report.n_trades:>11}{report.n_still_open_excluded:>10}{win_str:>8}"
            f"{report.net_pnl_ars:>16,.0f}{net_ci_str:>18}{exp_str:>15}{exp_ci_str:>20}"
        )
    lines.append("")
    lines.append(
        "* IC95% net_pnl_ars es expectancy_ci * n_trades (aproximado, ver docstring) - no un bootstrap "
        "directo sobre la suma. 'abiertos' = posiciones todavia abiertas al momento de esta corrida, "
        "excluidas del PnL realizado (ver n_still_open_excluded en metrics.py)."
    )
    return "\n".join(lines)


def section_invariant_violations(logs_dir: Path) -> str:
    """Violaciones de invariantes sobre logs/position_events.csv (reutiliza audit_journal_invariants.py)."""
    path = logs_dir / "position_events.csv"
    if not path.exists() or path.stat().st_size == 0:
        return "DATA INSUFFICIENT - logs/position_events.csv no existe o esta vacio."
    df = aji.load_journal(str(path))
    all_violations = []
    for check in aji.ALL_CHECKS:
        all_violations.extend(check(df))
    if not all_violations:
        return f"OK - 0 violaciones sobre {len(df)} filas del journal."
    by_sev: Dict[str, int] = Counter(v.severity for v in all_violations)
    lines = [f"{len(all_violations)} violaciones sobre {len(df)} filas del journal: {dict(by_sev)}", ""]
    for v in all_violations:
        lines.append(f"  [{v.severity}] {v.check}: {v.detail}")
    return "\n".join(lines)


def section_fills_outside_session(logs_dir: Path) -> str:
    """
    % de fills simulados (logs/shadow_trades.csv) cuyo timestamp cae fuera
    de la rueda BYMA asumida (ver ggal_bot/market_hours.py -
    is_within_byma_session). Mismo criterio que
    dashboard/data/market_hours_quality.py, aplicado sin pandas (ver
    docstring de load_shadow_fill_rows).
    """
    rows = load_shadow_fill_rows(logs_dir)
    if not rows:
        return "DATA INSUFFICIENT - logs/shadow_trades.csv no existe o esta vacio (o sin fills)."
    total = 0
    outside = 0
    outside_examples: List[str] = []
    for row in rows:
        ts_raw = row.get("timestamp_utc")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        total += 1
        if not is_within_byma_session(ts):
            outside += 1
            if len(outside_examples) < 10:
                outside_examples.append(f"{ts_raw} {row.get('symbol')} {row.get('side')} x{row.get('quantity')}")
    if total == 0:
        return "DATA INSUFFICIENT - ningun fill con timestamp parseable."
    pct = 100.0 * outside / total
    lines = [f"{outside}/{total} fills ({pct:.1f}%) fuera de la rueda BYMA asumida."]
    if outside_examples:
        lines.append("Ejemplos (hasta 10):")
        for ex in outside_examples:
            lines.append(f"  {ex}")
    return "\n".join(lines)


def section_valid_quote_pct_per_traded_symbol(logs_dir: Path, journal_rows: List[Dict]) -> str:
    """
    Para cada simbolo que REALMENTE se opero (aparece en
    logs/position_events.csv), % de filas de logs/market_snapshots.csv
    (esa misma base) donde la cotizacion es "operable" bajo el mismo
    criterio que el resto del proyecto: bid>0, ask>0, spread relativo <=
    RiskConfig.max_spread_relative (ver OrderBookSnapshot.is_tradeable).
    Un simbolo operado sin NINGUNA fila en market_snapshots.csv (por
    ejemplo si el logger de mercado no estaba activo ese dia) se reporta
    aparte, nunca se omite en silencio.
    """
    traded_symbols = sorted({r["symbol"] for r in journal_rows if r.get("symbol")})
    if not traded_symbols:
        return "DATA INSUFFICIENT - no hay simbolos operados en logs/position_events.csv."

    snapshot_rows = load_market_snapshot_rows(logs_dir)
    if not snapshot_rows:
        return (
            f"DATA INSUFFICIENT - logs/market_snapshots.csv no tiene filas (0 de {len(traded_symbols)} "
            "simbolos operados tienen cobertura de quotes). Simbolos operados: " + ", ".join(traded_symbols)
        )

    max_spread = SETTINGS.risk.max_spread_relative
    by_symbol: Dict[str, List[Dict]] = defaultdict(list)
    for row in snapshot_rows:
        sym = row.get("symbol")
        if sym:
            by_symbol[sym].append(row)

    lines = [f"Criterio de 'quote operable': bid>0, ask>0, spread relativo <= {max_spread:.1%}.", ""]
    lines.append(f"{'symbol':<16}{'n_quotes':>10}{'n_validas':>11}{'% validas':>11}")
    lines.append("-" * 48)
    no_coverage = []
    for sym in traded_symbols:
        rows = by_symbol.get(sym, [])
        if not rows:
            no_coverage.append(sym)
            continue
        n_valid = 0
        for r in rows:
            try:
                bid, ask = float(r.get("bid") or 0.0), float(r.get("ask") or 0.0)
            except (TypeError, ValueError):
                continue
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2.0
            if mid <= 0:
                continue
            if (ask - bid) / mid <= max_spread:
                n_valid += 1
        pct = 100.0 * n_valid / len(rows) if rows else 0.0
        lines.append(f"{sym:<16}{len(rows):>10}{n_valid:>11}{pct:>10.1f}%")
    if no_coverage:
        lines.append("")
        lines.append(
            f"SIN NINGUNA fila en market_snapshots.csv ({len(no_coverage)}/{len(traded_symbols)} simbolos "
            "operados): " + ", ".join(no_coverage)
        )
    return "\n".join(lines)


def section_backstop_triggers(logs_dir: Path) -> str:
    """Disparos del quote-gap backstop (ver load_backstop_trigger_lines/parse_backstop_triggers)."""
    lines_raw = load_backstop_trigger_lines(logs_dir)
    if not lines_raw:
        return "0 disparos encontrados en logs/ggal_bot.log (o el archivo no existe)."
    triggers = parse_backstop_triggers(lines_raw)
    by_reason: Dict[str, int] = Counter(reason for reason, _ in triggers)
    by_symbol: Dict[str, int] = Counter(symbol for _, symbol in triggers)
    out = [f"{len(triggers)} disparos totales. Por motivo: {dict(by_reason)}."]
    if by_symbol:
        top = ", ".join(f"{sym}x{n}" for sym, n in by_symbol.most_common(10))
        out.append(f"Por simbolo (top 10): {top}")
    unparsed = len(lines_raw) - len(triggers)
    if unparsed:
        out.append(f"ADVERTENCIA: {unparsed} lineas matchearon 'RESPALDO [...] disparado' pero no se pudieron parsear.")
    return "\n".join(out)


def section_h1_data_accumulation(logs_dir: Path) -> str:
    """
    Acumulacion de datos para Hipotesis 1 (RESEARCH.md): filas de
    market_snapshots.csv con last_price>0, y rango de fechas cubierto.
    `last_price` solo se loguea desde el commit 55d9bc2 (ver RESEARCH.md,
    correccion #5 de Hipotesis 1) - un total bajo o cero ES el resultado
    correcto mientras no haya pasado suficiente tiempo desde ese deploy,
    no un bug de este reporte.
    """
    rows = load_market_snapshot_rows(logs_dir)
    if not rows:
        return "DATA INSUFFICIENT - logs/market_snapshots.csv no tiene filas."
    total = len(rows)
    with_last_price = 0
    timestamps: List[str] = []
    for r in rows:
        try:
            lp = float(r.get("last_price") or 0.0)
        except (TypeError, ValueError):
            lp = 0.0
        if lp > 0:
            with_last_price += 1
            ts = r.get("timestamp_utc")
            if ts:
                timestamps.append(ts)
    pct = 100.0 * with_last_price / total if total else 0.0
    out = [f"{with_last_price}/{total} filas ({pct:.1f}%) con last_price>0."]
    if timestamps:
        out.append(f"Rango de esas filas: {min(timestamps)} .. {max(timestamps)}.")
    else:
        out.append("Ninguna fila con last_price>0 todavia - ver si el deploy con ese campo ya esta corriendo.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Orquestacion / CLI
# ---------------------------------------------------------------------------

def build_report(logs_dir: Path, as_of: Optional[datetime] = None) -> str:
    as_of = as_of or datetime.now(timezone.utc)
    journal_rows = load_position_events_rows(logs_dir)

    sections = [
        ("1) Trades y PnL neto por estrategia (con IC bootstrap)", section_trades_pnl_by_strategy(journal_rows)),
        ("2) Violaciones de invariantes (journal)", section_invariant_violations(logs_dir)),
        ("3) Fills fuera de horario de rueda", section_fills_outside_session(logs_dir)),
        ("4) % de quotes validas por contrato operado", section_valid_quote_pct_per_traded_symbol(logs_dir, journal_rows)),
        ("5) Disparos del respaldo de ultimo resorte (backstop)", section_backstop_triggers(logs_dir)),
        ("6) Acumulacion de datos para Hipotesis 1 (last_price>0)", section_h1_data_accumulation(logs_dir)),
    ]

    out = [f"# Revision semanal GGAL_BOT — {as_of.date().isoformat()} ({as_of.strftime('%H:%M UTC')})", ""]
    out.append(f"Logs leidos de: `{logs_dir}`")
    out.append("")
    for title, body in sections:
        out.append(f"## {title}")
        out.append("")
        out.append(body)
        out.append("")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--logs-dir", type=Path, default=paths.LOGS_DIR, help="Directorio de logs a auditar (default: logs/ del proyecto).")
    parser.add_argument("--out", type=Path, default=None, help="Archivo de salida (default: weekly_review_<fecha>.md en el directorio actual).")
    args = parser.parse_args(argv)

    report = build_report(args.logs_dir)
    print(report)

    out_path = args.out or Path(f"weekly_review_{date.today().isoformat()}.md")
    out_path.write_text(report, encoding="utf-8")
    print(f"\nReporte escrito en {out_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
