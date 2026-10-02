"""
quantify_historical_mock_fills.py
===================================
Herramienta ADMINISTRATIVA (no un test automatico: necesita datos REALES de
produccion, no fixtures) para el item 3(c) de la Tarea #27/#28 (a pedido
explicito del usuario, 2026-10-02): "Cuantifica cuantos fills historicos se
hicieron con mock, incluidos los de horario de rueda".

POR QUE ESTO ES UN SCRIPT SEPARADO Y NO UN NUMERO YA CALCULADO: este
sandbox de desarrollo NUNCA tuvo acceso al shadow_trades.csv/ggal_bot.log
REALES de produccion (Northflank) - solo a copias locales/fixtures de
tests, aisladas deliberadamente del archivo real (ver
ggal_bot/validation/_shadow_audit_isolation.py). Cuantificar esto con datos
inventados violaria la regla mas basica de este seguimiento (nunca fabricar
datos/resultados). Este script esta pensado para que lo corras VOS, contra
tus archivos reales (descargados del volumen de Northflank o ya presentes
en tu copia local), y el resultado sea 100% trazable a esos archivos.

LIMITACION EXPLICITA, IMPORTANTE: antes del deploy de
ggal_bot/data/market_data_source_log.py (Tarea #27/#28 item 3(b), este
mismo seguimiento), NINGUN archivo estructurado registraba la fuente activa
fill por fill - shadow_trades.csv/position_events.csv nunca tuvieron esa
columna (y deliberadamente no la van a tener, ver el docstring de
market_data_source_log.py). La UNICA evidencia disponible para fills
ANTERIORES a ese deploy son las lineas de texto que
ggal_bot/data/live_shadow_feed.py ya logueaba en cada cambio de fuente
("Shadow feed: fuente activa = ...", "Shadow feed: failover ... -> fuente
...", etc. - ver _TRANSITION_PATTERNS abajo, que replican EXACTAMENTE esos
formatos de log, verificados contra el codigo fuente real del modulo). Este
script reconstruye, a partir de esas lineas, una linea de tiempo de "que
fuente estuvo activa entre que momento y que momento" y la cruza contra el
timestamp de cada fill real.

Esto es, por construccion, MENOS confiable que leer una columna estructurada
(exactamente el problema que motiva el item 3(b) - de ahi en mas, un fill
nuevo no necesita este script): si el log fue rotado/truncado y no incluye
la transicion de fuente mas reciente ANTES del primer fill del rango
analizado, ese fill (y cualquiera antes de la primera transicion visible en
el log) queda marcado como "DESCONOCIDA" en vez de adivinado. Pasa el/los
archivo(s) de log mas completo(s) que tengas (Northflank conserva stdout
del contenedor; si rota, pasa todos los fragmentos con --log repetido o un
glob) para minimizar este hueco.

Uso tipico:

    python -m ggal_bot.ops.quantify_historical_mock_fills \
        --log logs/ggal_bot.log \
        --shadow-trades logs/shadow_trades.csv \
        --out mock_fills_report.csv

Con varios fragmentos de log (rotacion):

    python -m ggal_bot.ops.quantify_historical_mock_fills \
        --log logs/ggal_bot.log.1 --log logs/ggal_bot.log \
        --shadow-trades logs/shadow_trades.csv

Imprime un resumen (conteo de fills por fuente, cuantos con MockReplaySource
en total, y cuantos de esos cayeron DENTRO del horario de rueda asumido -
ver ggal_bot/market_hours.py - que es el subconjunto mas grave: fills
fabricados que un vistazo rapido al reloj no delataria como sospechosos) y,
si se pasa --out, un CSV detallado fill por fill.
"""
from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from ggal_bot import market_hours
from ggal_bot.paths import SHADOW_TRADES_LOG

# Formato real del log (ver run_bot.py: logging.Formatter("%(asctime)s
# [%(levelname)s] %(name)s: %(message)s"), asctime en hora ART (UTC-3 fijo,
# ver _art_time_converter) - NO UTC. Ejemplo real:
#   "2026-09-30 14:22:10,123 [WARNING] ggal_bot.data.live_shadow_feed: Shadow feed: failover ..."
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[(?P<level>\w+)\] "
    r"(?P<logger>[\w.]+): (?P<message>.*)$"
)

ART_OFFSET_HOURS = market_hours.ART_OFFSET_HOURS  # -3.0, misma fuente de verdad que el resto del proyecto

# Cada patron de abajo replica, EXACTAMENTE, un mensaje real de
# ggal_bot/data/live_shadow_feed.py (verificado contra el codigo fuente de
# ese modulo, no adivinado) que indica un cambio de fuente activa.
_RE_INITIAL_ACTIVE = re.compile(r"^Shadow feed: fuente activa = '(?P<name>[^']+)' \(prioridad \d+/\d+\)\.$")
_RE_FAILOVER = re.compile(
    r"^Shadow feed: failover \(fallaron \d+ polls consecutivos\) -> fuente '(?P<name>[^']+)' \(prioridad \d+/\d+\)\.$"
)
_RE_PRIORITY_EXHAUSTED = re.compile(
    r"^Shadow feed: se agotaron todas las fuentes configuradas en source_priority .*; "
    r"se intenta failover final a Mock/Replay\.$"
)
_RE_MOCK_FALLBACK_DISABLED = re.compile(
    r"^Shadow feed: .* y el fallback a Mock/Replay esta deshabilitado .*$"
)
_RE_REPROBE_RETURN = re.compile(
    r"^Shadow feed: '(?P<name>[^']+)' \(mayor prioridad que la fuente activa\) volvio a estar disponible; "
    r"se vuelve a esa fuente\.$"
)
_RE_MOCK_MISCONFIGURED = re.compile(r"^Shadow feed: fuente 'mock' no se pudo instanciar .*$")

_UNKNOWN_SOURCE = "DESCONOCIDA (sin transicion visible en el log antes de este fill)"


@dataclass
class SourceTransition:
    timestamp_utc: datetime
    source_name: str
    raw_line: str


def _parse_art_timestamp_to_utc(ts_str: str) -> datetime:
    naive_art = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
    return naive_art.replace(tzinfo=timezone.utc) - timedelta(hours=ART_OFFSET_HOURS)


def parse_source_transitions(log_paths: List[Path]) -> Tuple[List[SourceTransition], List[str]]:
    """
    Devuelve (transiciones ordenadas cronologicamente, lineas de advertencia
    de misconfiguracion de mock encontradas - informativas, no cambian la
    fuente activa) a partir de uno o mas archivos de log reales.
    """
    transitions: List[SourceTransition] = []
    misconfig_warnings: List[str] = []
    pending_exhausted_at: Optional[datetime] = None

    for log_path in log_paths:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        for raw_line in text.splitlines():
            m = _LOG_LINE_RE.match(raw_line)
            if not m:
                continue
            if m.group("logger") != "ggal_bot.data.live_shadow_feed":
                continue
            message = m.group("message")
            try:
                ts_utc = _parse_art_timestamp_to_utc(m.group("ts"))
            except ValueError:
                continue

            if pending_exhausted_at is not None:
                # La linea INMEDIATAMENTE siguiente de este logger decide si
                # el fallback incondicional a Mock tuvo exito (sin mensaje
                # adicional - ver _construct_mock_or_fallback) o fallo
                # (ERROR de "fallback a Mock/Replay esta deshabilitado").
                if _RE_MOCK_FALLBACK_DISABLED.match(message):
                    transitions.append(SourceTransition(pending_exhausted_at, "_NoDataSource", raw_line))
                else:
                    transitions.append(SourceTransition(pending_exhausted_at, "MockReplaySource", raw_line))
                pending_exhausted_at = None
                # no "continue": la linea actual todavia puede matchear otro patron propio

            if (mm := _RE_INITIAL_ACTIVE.match(message)):
                transitions.append(SourceTransition(ts_utc, mm.group("name"), raw_line))
            elif (mm := _RE_FAILOVER.match(message)):
                transitions.append(SourceTransition(ts_utc, mm.group("name"), raw_line))
            elif (mm := _RE_REPROBE_RETURN.match(message)):
                transitions.append(SourceTransition(ts_utc, mm.group("name"), raw_line))
            elif _RE_PRIORITY_EXHAUSTED.match(message):
                pending_exhausted_at = ts_utc
            elif _RE_MOCK_MISCONFIGURED.match(message):
                misconfig_warnings.append(f"{m.group('ts')} ART: {raw_line}")

    if pending_exhausted_at is not None:
        # "se agotaron..." fue la ULTIMA linea de este logger en todo el
        # material provisto (ningun log posterior confirma o desmiente el
        # fallback) - se asume el comportamiento historico real (antes de
        # ShadowConfig.allow_mock_source, Tarea #27/#28 item 3(a): el
        # fallback a Mock SIEMPRE tenia exito, incondicional, sin ningun
        # flag que pudiera deshabilitarlo).
        transitions.append(SourceTransition(pending_exhausted_at, "MockReplaySource", "<fin del material de log>"))

    transitions.sort(key=lambda t: t.timestamp_utc)
    return transitions, misconfig_warnings


def active_source_at(transitions: List[SourceTransition], ts_utc: datetime) -> str:
    """Ultima transicion con timestamp <= ts_utc; _UNKNOWN_SOURCE si ninguna la precede."""
    active = _UNKNOWN_SOURCE
    for t in transitions:
        if t.timestamp_utc <= ts_utc:
            active = t.source_name
        else:
            break
    return active


class QuantifyMockFillsUnavailable(RuntimeError):
    """dashboard.pnl_engine (pandas/numpy) no esta disponible en este entorno."""


def _load_fills(csv_path: Path):
    try:
        from dashboard.pnl_engine import load_fills
    except ImportError as exc:
        raise QuantifyMockFillsUnavailable(
            "No se pudo importar dashboard.pnl_engine (falta pandas/numpy en este "
            "entorno - ver requirements-dashboard.txt) para leer shadow_trades.csv."
        ) from exc
    return load_fills(csv_path)


@dataclass
class FillSourceRow:
    timestamp_utc: datetime
    client_order_id: str
    symbol: str
    active_source: str
    within_byma_session: bool


def quantify(
    log_paths: List[Path], shadow_trades_csv: Path,
) -> Tuple[List[FillSourceRow], List[str]]:
    transitions, misconfig_warnings = parse_source_transitions(log_paths)
    fills_df = _load_fills(shadow_trades_csv)

    rows: List[FillSourceRow] = []
    for row in fills_df.itertuples(index=False):
        ts = getattr(row, "timestamp_utc").to_pydatetime()
        source = active_source_at(transitions, ts)
        rows.append(FillSourceRow(
            timestamp_utc=ts,
            client_order_id=getattr(row, "client_order_id", ""),
            symbol=getattr(row, "symbol", ""),
            active_source=source,
            within_byma_session=market_hours.is_within_byma_session(ts),
        ))
    return rows, misconfig_warnings


def format_report(rows: List[FillSourceRow], misconfig_warnings: List[str]) -> str:
    total = len(rows)
    lines = [f"Fuente activa reconstruida para {total} fill(s) de shadow_trades.csv"]
    if total == 0:
        lines.append("Sin fills para analizar.")
        return "\n".join(lines)

    by_source: dict = {}
    for r in rows:
        by_source.setdefault(r.active_source, []).append(r)

    lines.append("\nPor fuente activa:")
    for source, source_rows in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"  {source}: {len(source_rows)} fill(s)")

    mock_rows = by_source.get("MockReplaySource", [])
    mock_in_session = [r for r in mock_rows if r.within_byma_session]
    mock_out_session = [r for r in mock_rows if not r.within_byma_session]
    lines.append(f"\nFills con MockReplaySource (datos 100% sinteticos): {len(mock_rows)}/{total}")
    lines.append(
        f"  - DURANTE el horario de rueda asumido (11:00-17:00 ART Lun-Vie, ver "
        f"ggal_bot/market_hours.py): {len(mock_in_session)} - el caso mas grave: parecen fills "
        f"normales a simple vista pero son datos fabricados."
    )
    lines.append(f"  - fuera de ese horario: {len(mock_out_session)}")

    unknown_rows = by_source.get(_UNKNOWN_SOURCE, [])
    if unknown_rows:
        lines.append(
            f"\nNOTA: {len(unknown_rows)} fill(s) sin ninguna transicion de fuente visible en "
            "el/los log(s) provisto(s) ANTES de su timestamp (posible rotacion/log incompleto) "
            "- no se pudieron clasificar, quedan como DESCONOCIDA en vez de adivinados."
        )

    if misconfig_warnings:
        lines.append(
            f"\nADVERTENCIA: se encontraron {len(misconfig_warnings)} intento(s) de usar 'mock' "
            "explicito en source_priority sin GGAL_BOT_ALLOW_MOCK_SOURCE=true (ver ShadowConfig."
            "allow_mock_source) - no cambiaron la fuente activa (se ignoraron), pero revisar la "
            "configuracion del deploy en esas fechas:"
        )
        for w in misconfig_warnings[:10]:
            lines.append(f"  - {w}")
        if len(misconfig_warnings) > 10:
            lines.append(f"  ... y {len(misconfig_warnings) - 10} mas.")

    return "\n".join(lines)


def _write_detail_csv(rows: List[FillSourceRow], out_path: Path) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_utc", "client_order_id", "symbol", "active_source", "within_byma_session"])
        for r in rows:
            writer.writerow([r.timestamp_utc.isoformat(), r.client_order_id, r.symbol, r.active_source, r.within_byma_session])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--log", action="append", required=True, dest="logs",
        help="Ruta a un archivo de log real (ggal_bot.log). Repetir --log para varios fragmentos (rotacion).",
    )
    parser.add_argument("--shadow-trades", default=str(SHADOW_TRADES_LOG), help="Ruta a shadow_trades.csv real.")
    parser.add_argument("--out", default=None, help="Si se pasa, escribe el detalle fill-por-fill a este CSV.")
    args = parser.parse_args(argv)

    log_paths = [Path(p) for p in args.logs]
    missing = [p for p in log_paths if not p.exists()]
    if missing:
        print(f"ERROR: no existe(n): {', '.join(str(p) for p in missing)}")
        return 2

    try:
        rows, misconfig_warnings = quantify(log_paths, Path(args.shadow_trades))
    except QuantifyMockFillsUnavailable as exc:
        print(f"ERROR: {exc}")
        return 2

    print(format_report(rows, misconfig_warnings))
    if args.out:
        _write_detail_csv(rows, Path(args.out))
        print(f"\nDetalle fill-por-fill escrito en {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
