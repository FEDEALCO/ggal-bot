"""
weekend_guard_check.py
=========================
Herramienta ADMINISTRATIVA (no un test automatico: necesita datos REALES de
produccion, no fixtures) para verificar, el lunes siguiente, si el viernes
anterior el bot abrio alguna posicion NUEVA de weekly_asymmetric que
`RiskConfig.weekend_theta_guard_block_new_entries` deberia haber bloqueado.

CONTEXTO (a pedido explicito del usuario, sesion 2026-10-01: "Mañana viernes
es la primera prueba del fix del weekend guard: dejá listo un chequeo que
confirme el lunes que no hubo entradas semanales nuevas el viernes"):

`weekend_theta_guard_enabled` (ver config.LongFirstConfig, default True)
cierra, TODOS los viernes, cualquier posicion de weekly_asymmetric cuyo
vencimiento no haya llegado todavia - sin importar cuantos dias lleva
abierta (ver risk/risk_manager.py::evaluate_position_exit, motivo
"weekend_theta_guard"). Hasta el FIX 2026-09-29, `scan_entry_signals()` no
sabia que dia era, asi que nada le impedia abrir una posicion NUEVA un
viernes sobre un vencimiento posterior a ese viernes - esa posicion queda
con holding_business_days=0 y el guard de salida la cierra casi de
inmediato en el siguiente ciclo de riesgo. VERIFICADO contra la muestra de
Fase 0: esto produjo 194/199 (97.5%) de las entradas de weekly_asymmetric -
abiertas y cerradas el MISMO viernes en una mediana de 23 segundos, pagando
el costo de round-trip completo (-609.077 ARS neto) sobre un PnL bruto casi
nulo (+9.904 ARS) - ~85% de la perdida neta total de la estrategia en esa
muestra (ver docstring completo de `weekend_theta_guard_block_new_entries`
en ggal_bot/config.py).

El fix es OPT-IN (`GGAL_BOT_WEEKEND_THETA_GUARD_BLOCK_NEW_ENTRIES`, default
False): bloquea una entrada nueva de weekly_asymmetric SOLO si es viernes
(ART) Y el vencimiento de esa base es posterior a ese viernes. Este modulo
verifica, con los eventos REALES del Event Journal, si efectivamente NO se
abrio ninguna posicion que cumpliera esa condicion el viernes indicado -
una lista vacia es el resultado ESPERADO si el fix esta funcionando (o si
nunca llegaron candidatas que lo activaran ese dia en particular, lo cual
tambien es un resultado valido, no un "PASS" fabricado).

Uso tipico (lunes por la mañana, contra el CSV descargado del dashboard -
ver los botones de descarga en dashboard/app.py, o directo del volumen de
Northflank):

    python -m ggal_bot.ops.weekend_guard_check \
        --position-events logs/position_events.csv \
        --friday 2026-10-02

Sin --friday, usa el viernes mas reciente (hoy mismo si hoy es viernes).
Exit code 0 si no se encontraron violaciones, 1 si se encontro al menos una
(para poder encadenarlo en un script/cron que avise por su propio canal).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from ggal_bot.market_hours import ART_OFFSET_HOURS
from ggal_bot.paths import POSITION_EVENTS_LOG


class WeekendGuardCheckUnavailable(RuntimeError):
    """dashboard.pnl_engine (pandas/numpy) no esta disponible en este entorno."""


@dataclass
class WeekendGuardViolation:
    timestamp_utc: object  # datetime o pandas.Timestamp, segun el llamador
    symbol: str
    position_id: str
    expiry: date
    order_client_id: str


def most_recent_friday(today: Optional[date] = None) -> date:
    """Viernes mas reciente (inclusive si `today` ya es viernes), en
    calendario ART - mismo criterio del resto de este chequeo."""
    today = today if today is not None else _now_art_date()
    offset = (today.weekday() - 4) % 7
    return today - timedelta(days=offset)


def _now_art_date() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=ART_OFFSET_HOURS)).date()


def _to_art_date(ts_utc) -> date:
    """
    Fecha calendario en ART (UTC-3 fijo, sin horario de verano desde 2009 -
    mismo SUPUESTO explicito no verificado que ggal_bot.market_hours) de un
    timestamp UTC (datetime o pandas.Timestamp, ambos soportados).
    """
    py_ts = ts_utc.to_pydatetime() if hasattr(ts_utc, "to_pydatetime") else ts_utc
    if py_ts.tzinfo is None:
        py_ts = py_ts.replace(tzinfo=timezone.utc)
    art_ts = py_ts.astimezone(timezone.utc) + timedelta(hours=ART_OFFSET_HOURS)
    return art_ts.date()


def _parse_expiry_from_contract_key(contract_key) -> Optional[date]:
    """
    contract_key = "underlying|symbol|expiry_iso" (ver run_bot.py,
    Position.contract_key - ej. "GGAL|GFGC6600OC|2026-10-16"). None si esta
    vacio/no tiene ese formato - nunca se adivina un vencimiento sin
    evidencia (ver data_unavailable_fields en el Event Journal).
    """
    if not contract_key or not isinstance(contract_key, str):
        return None
    parts = contract_key.split("|")
    if len(parts) != 3:
        return None
    try:
        return date.fromisoformat(parts[2])
    except ValueError:
        return None


def find_friday_entries_that_should_have_been_blocked(
    position_events_df, friday: date, strategy_tag: str = "weekly_asymmetric",
) -> List[WeekendGuardViolation]:
    """
    Eventos ENTRY de `strategy_tag` cuyo timestamp cae en `friday`
    (calendario ART) Y cuyo vencimiento (via contract_key) es POSTERIOR a
    `friday` - exactamente la condicion que
    `weekend_theta_guard_block_new_entries` deberia bloquear (ver
    weekly_asymmetric.py::scan_entry_signals, parametro `now`). Una lista
    vacia es el resultado ESPERADO si el fix esta funcionando.

    Entradas sin contract_key parseable se EXCLUYEN de esta lista (no hay
    evidencia de su vencimiento para juzgarlas) - ver
    entries_without_expiry_on_friday() para recuperarlas aparte.
    """
    if position_events_df.empty:
        return []
    df = position_events_df
    mask = (df["event_type"] == "ENTRY") & (df["strategy_tag"] == strategy_tag)
    candidates = df[mask]
    violations: List[WeekendGuardViolation] = []
    for row in candidates.itertuples(index=False):
        ts = getattr(row, "timestamp_utc")
        if _to_art_date(ts) != friday:
            continue
        expiry = _parse_expiry_from_contract_key(getattr(row, "contract_key", ""))
        if expiry is None or expiry <= friday:
            continue
        violations.append(WeekendGuardViolation(
            timestamp_utc=ts, symbol=getattr(row, "symbol", ""),
            position_id=getattr(row, "position_id", ""), expiry=expiry,
            order_client_id=getattr(row, "order_client_id", ""),
        ))
    return violations


def entries_without_expiry_on_friday(
    position_events_df, friday: date, strategy_tag: str = "weekly_asymmetric",
):
    """
    Entradas de `strategy_tag` el `friday` (ART) SIN contract_key parseable:
    no se puede evaluar si violan el guard, asi que se reportan aparte en
    vez de desaparecer en silencio de find_friday_entries_that_should_have_
    been_blocked().
    """
    if position_events_df.empty:
        return position_events_df
    df = position_events_df
    mask = (df["event_type"] == "ENTRY") & (df["strategy_tag"] == strategy_tag)
    candidates = df[mask].copy()
    if candidates.empty:
        return candidates
    candidates["_art_date"] = candidates["timestamp_utc"].apply(_to_art_date)
    candidates = candidates[candidates["_art_date"] == friday]
    candidates = candidates[candidates["contract_key"].apply(_parse_expiry_from_contract_key).isna()]
    return candidates.drop(columns=["_art_date"])


def format_report(violations: List[WeekendGuardViolation], friday: date, unknown_count: int) -> str:
    lines = [f"Chequeo weekend guard - viernes {friday.isoformat()} (ART)"]
    if not violations:
        lines.append(
            "RESULTADO: OK - no se encontro ninguna entrada nueva de weekly_asymmetric "
            "ese viernes con vencimiento posterior a ese mismo viernes."
        )
    else:
        lines.append(
            f"RESULTADO: FALLO - se encontraron {len(violations)} entrada(s) que el guard "
            "deberia haber bloqueado:"
        )
        for v in violations:
            lines.append(
                f"  - {v.symbol} (position_id={v.position_id}, order_client_id={v.order_client_id}): "
                f"ENTRY {v.timestamp_utc} UTC, vencimiento {v.expiry.isoformat()}"
            )
        lines.append(
            "Verificar si GGAL_BOT_WEEKEND_THETA_GUARD_BLOCK_NEW_ENTRIES esta realmente "
            "seteado en el deploy (default False - sin el env var explicito, el codigo ya "
            "soporta el fix pero no esta activo)."
        )
    if unknown_count:
        lines.append(
            f"NOTA: {unknown_count} entrada(s) de weekly_asymmetric ese viernes sin "
            "contract_key parseable (dato no disponible) - no se pudieron evaluar, revisar a mano."
        )
    return "\n".join(lines)


def _load_position_events(csv_path):
    try:
        from dashboard.pnl_engine import load_position_events
    except ImportError as exc:
        raise WeekendGuardCheckUnavailable(
            "No se pudo importar dashboard.pnl_engine (falta pandas/numpy en este "
            "entorno - ver requirements-dashboard.txt) para leer el Event Journal."
        ) from exc
    return load_position_events(csv_path)


def run_check(position_events_csv: Optional[Path] = None, friday: Optional[date] = None) -> int:
    """
    Corre el chequeo completo e imprime el reporte. Devuelve el exit code
    (0 = OK, 1 = se encontraron violaciones) para uso en linea de comandos
    o encadenado en otro script.
    """
    path = position_events_csv if position_events_csv is not None else POSITION_EVENTS_LOG
    target_friday = friday if friday is not None else most_recent_friday()

    position_events_df = _load_position_events(path)
    violations = find_friday_entries_that_should_have_been_blocked(position_events_df, target_friday)
    unknown = entries_without_expiry_on_friday(position_events_df, target_friday)

    print(format_report(violations, target_friday, len(unknown)))
    return 1 if violations else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--position-events", default=str(POSITION_EVENTS_LOG), help="Ruta a position_events.csv")
    parser.add_argument("--friday", default=None, help="YYYY-MM-DD (ART). Default: el viernes mas reciente.")
    args = parser.parse_args(argv)

    friday = date.fromisoformat(args.friday) if args.friday else None
    try:
        return run_check(Path(args.position_events), friday)
    except WeekendGuardCheckUnavailable as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
