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

ACTUALIZACION 2026-10-02 (a pedido explicito del usuario, tras el hallazgo
en logs de produccion del 2026-10-02 - kill switch de cartera disparado:
"Agregá a weekend_guard_check que distinga 'sin entradas porque el guard
bloqueó' de 'sin entradas por kill switch u otra causa', leyendo los
REJECT y el estado del kill switch. Si no, el lunes un OK no prueba nada"):

El RESULTADO OK original (ninguna entrada CONFIRMADA que violara el guard)
es necesario pero NO suficiente: si el kill switch ya estaba disparado, o
si el limite de Griegas (duro o preventivo) ya frenaba toda entrada nueva
de weekly_asymmetric ese mismo viernes, NINGUNA señal habria llegado a
abrirse de todos modos, con o sin el weekend guard - un OK en ese escenario
no es evidencia de que el guard haya hecho su trabajo, es solo ausencia de
dato. El guard de fin de semana en si (weekend_theta_guard_block_new_entries)
bloquea ANTES de que la señal se genere (ver
strategy/weekly_asymmetric.py::scan_entry_signals) y por eso NUNCA deja un
evento REJECT en el Event Journal - es, precisamente, esa ausencia la que
permite la distincion: se buscan los REJECT de entrada (side="buy") de
weekly_asymmetric ese viernes (kill_switch_tripped/greeks_limit_exceeded/
greeks_budget_preemptive/unknown_greeks/sizing_not_tradeable - ver
run_bot.py::_act_on_entry_signal) y el estado ACTUAL del kill switch
(ultimo trip conocido, NO un historial completo - ver
ggal_bot/risk/kill_switch.py, que solo persiste el ULTIMO trip) para
avisar explicitamente cuando un OK no es una prueba real.

ACTUALIZACION 2026-10-05 (bug real de severidad del aviso, a pedido
explicito del usuario - episodio REAL del viernes 2026-10-02: el kill
switch disparo ese mismo dia y quedo activo durante gran parte de la
jornada, con 49 REJECT por kill_switch_tripped en el Event Journal real,
repartidos entre weekly_asymmetric Y scalping): el aviso de REJECTs de
ACTUALIZACION 2026-10-02 (parrafo anterior) solo mira REJECTs de
weekly_asymmetric especificamente, y los reporta como una nota al pie
despues del RESULTADO OK/FALLO - una lectura rapida del reporte podia
quedarse solo con el "RESULTADO: OK" y no leer la nota de mas abajo. Con
el caso real del 2026-10-02, eso significa que un chequeo de ese dia
podria leerse como "el guard funciono" cuando en realidad el kill switch
bloqueaba TODO, con o sin el guard - la "prueba" nunca llego a ocurrir.
Fix: find_kill_switch_rejects_on_day_any_strategy() mira TODO REJECT por
kill_switch_tripped ese dia (de CUALQUIER estrategia, no solo
weekly_asymmetric) y, si encuentra al menos uno,
kill_switch_invalidates_day_note() genera un aviso EXPLICITO que
format_report() imprime PRIMERO, antes del RESULTADO OK/FALLO - no una
nota al pie mas.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ggal_bot.market_hours import ART_OFFSET_HOURS
from ggal_bot.paths import KILL_SWITCH_STATE_FILE, POSITION_EVENTS_LOG
from ggal_bot.risk.kill_switch import KillSwitch, KillSwitchState

# Prefijos REALES de reason= en los REJECT de ENTRADA (side="buy") que
# run_bot.py::_act_on_entry_signal puede loguear - ver el modulo para cada
# call site exacto. "weekend_entry_guard" DELIBERADAMENTE no esta en esta
# lista: ese bloqueo ocurre ANTES de generar la señal (scan_entry_signals),
# nunca llega a loguearse como REJECT - su ausencia total en el Event
# Journal es justamente la señal que distingue "la bloqueo el guard" de
# "nunca llego a intentarse".
_ENTRY_REJECT_REASON_BUCKETS = {
    "kill_switch_tripped": "kill_switch",
    "greeks_limit_exceeded": "greeks_limit",
    "greeks_budget_preemptive": "greeks_limit",
    "unknown_greeks": "greeks_limit",
    "sizing_not_tradeable": "sizing",
}


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


def find_entry_rejects_on_day(
    position_events_df, day: date, strategy_tag: str = "weekly_asymmetric",
):
    """
    Eventos REJECT de ENTRADA (side="buy") de `strategy_tag` cuyo timestamp
    cae en `day` (calendario ART) - a diferencia de
    find_friday_entries_that_should_have_been_blocked() (que mira ENTRY ya
    CONFIRMADOS), esto captura señales que ni siquiera llegaron a abrirse,
    por cualquier motivo DISTINTO del weekend guard (ver
    _ENTRY_REJECT_REASON_BUCKETS y el docstring del modulo para por que el
    guard en si nunca aparece aca).
    """
    if position_events_df.empty:
        return position_events_df
    df = position_events_df
    mask = (
        (df["event_type"] == "REJECT")
        & (df["strategy_tag"] == strategy_tag)
        & (df["side"] == "buy")
    )
    candidates = df[mask]
    if candidates.empty:
        return candidates
    return candidates[candidates["timestamp_utc"].apply(_to_art_date) == day]


def find_kill_switch_rejects_on_day_any_strategy(position_events_df, day: date):
    """
    MEJORA 2026-10-05 (a pedido explicito del usuario, tras el episodio
    REAL del 2026-10-02: el kill switch disparo ese mismo viernes y se
    mantuvo activo durante gran parte del dia - 49 REJECT por
    kill_switch_tripped en el Event Journal real de ese dia, repartidos
    entre weekly_asymmetric Y scalping, no solo weekly_asymmetric).

    A diferencia de find_entry_rejects_on_day() (que mira SOLO entradas de
    `strategy_tag` especifico, tipicamente "weekly_asymmetric" - el unico
    que interesa para evaluar si el weekend guard hizo falta), esto mira
    TODO REJECT de motivo kill_switch_tripped ese dia, sin filtrar por
    estrategia ni por side - porque lo que importa aca es una pregunta
    distinta: "¿hubo evidencia, en cualquier parte del Event Journal de
    ese dia, de que el kill switch estuvo activo?" - si la respuesta es
    si, NINGUN resultado de este chequeo (ni OK ni FALLO) es una prueba
    valida sobre el weekend guard en particular: el kill switch bloqueaba
    TODO, con o sin el guard. Ver run_check()/format_report() para como
    esto se convierte en un aviso explicito de "este dia no es una prueba
    valida", no solo una nota al pie.
    """
    if position_events_df.empty:
        return position_events_df
    df = position_events_df
    reason_str = df["reason"].fillna("").astype(str)
    mask = (df["event_type"] == "REJECT") & reason_str.str.startswith("kill_switch_tripped")
    candidates = df[mask]
    if candidates.empty:
        return candidates
    return candidates[candidates["timestamp_utc"].apply(_to_art_date) == day]


def kill_switch_invalidates_day_note(kill_switch_rejects_df) -> Optional[str]:
    """
    None si no hubo ningun REJECT por kill_switch_tripped ese dia (ver
    find_kill_switch_rejects_on_day_any_strategy) - en ese caso no hay
    evidencia de que el kill switch haya invalidado el dia, y no se
    fabrica una advertencia sin base. Si hubo al menos uno, devuelve un
    texto EXPLICITO (no una nota al pie mas) marcando que el resultado de
    ESTE chequeo, para ESTE dia, no prueba nada sobre el weekend guard -
    con la primera y ultima marca de tiempo confirmadas como evidencia
    concreta de la ventana de tiempo cubierta (nunca se afirma "todo el
    dia" sin evidencia - solo se reporta la ventana REAL observada en el
    Event Journal, que puede ser un subconjunto del dia si el kill switch
    se reseteo y volvio a disparar).
    """
    if kill_switch_rejects_df.empty:
        return None
    first_ts = kill_switch_rejects_df["timestamp_utc"].min()
    last_ts = kill_switch_rejects_df["timestamp_utc"].max()
    n = len(kill_switch_rejects_df)
    return (
        f"⚠️ ESTE DIA NO ES UNA PRUEBA VALIDA DEL WEEKEND GUARD: el Event Journal registra "
        f"{n} señal(es) rechazada(s) por kill_switch_tripped ese mismo dia (de cualquier "
        f"estrategia), entre {first_ts} y {last_ts} UTC. Mientras el kill switch estuvo "
        "disparado, TODA entrada nueva se bloqueaba de todos modos, con o sin el weekend "
        "guard - el resultado de arriba (OK o FALLO) no distingue cual de los dos fue la "
        "causa real de la ausencia (o presencia) de entradas en esa ventana."
    )


def _reject_reason_bucket(reason: str) -> str:
    prefix = reason.split(":", 1)[0].strip() if reason else ""
    return _ENTRY_REJECT_REASON_BUCKETS.get(prefix, "other")


def summarize_entry_rejects(rejects_df) -> Dict[str, int]:
    """{bucket: cantidad} de los REJECT de entrada pasados - ver
    _reject_reason_bucket. Dict vacio si no hay ninguno."""
    counts: Dict[str, int] = {}
    if rejects_df.empty:
        return counts
    for reason in rejects_df["reason"].fillna(""):
        bucket = _reject_reason_bucket(str(reason))
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def kill_switch_status_note(friday: date, kill_switch_state: Optional[KillSwitchState]) -> Optional[str]:
    """
    Texto de advertencia si el ULTIMO trip CONOCIDO del kill switch cae en
    o antes de `friday` - `kill_switch_state` solo guarda el trip mas
    reciente (ver ggal_bot/risk/kill_switch.py: nunca un historial
    completo), asi que esto NUNCA afirma que estuvo disparado TODO el dia,
    solo que hay evidencia parcial de que pudo estarlo. None si el kill
    switch no esta disparado, o si su ultimo trip conocido es POSTERIOR a
    `friday` (no es evidencia relevante para ese dia en particular).
    """
    if kill_switch_state is None or not kill_switch_state.tripped:
        return None
    tripped_at_date: Optional[date] = None
    if kill_switch_state.tripped_at:
        try:
            tripped_at_date = _to_art_date(datetime.fromisoformat(kill_switch_state.tripped_at))
        except ValueError:
            tripped_at_date = None
    if tripped_at_date is not None and tripped_at_date > friday:
        return None
    since = f", desde {tripped_at_date.isoformat()} (ART)" if tripped_at_date else " (fecha de disparo desconocida)"
    return (
        f"Kill switch ACTUALMENTE disparado ({kill_switch_state.tripped_by}: {kill_switch_state.reason})"
        f"{since}. Esto es el estado ACTUAL/ultimo conocido, NO un historial completo del "
        "viernes evaluado - si cubrio ese dia, un RESULTADO OK de arriba no prueba que el "
        "weekend guard haya sido la causa real de la ausencia de entradas."
    )


def format_report(
    violations: List[WeekendGuardViolation],
    friday: date,
    unknown_count: int,
    reject_counts: Optional[Dict[str, int]] = None,
    kill_switch_note: Optional[str] = None,
    kill_switch_state_available: bool = True,
    kill_switch_invalidates_note: Optional[str] = None,
) -> str:
    reject_counts = reject_counts or {}
    lines = [f"Chequeo weekend guard - viernes {friday.isoformat()} (ART)"]

    # NUEVO 2026-10-05 (a pedido explicito del usuario, ver
    # kill_switch_invalidates_day_note): esto va PRIMERO, antes incluso del
    # RESULTADO OK/FALLO - si el kill switch estuvo activo ese dia, quien
    # lea el reporte tiene que verlo antes de leer cualquier otra cosa, no
    # como una nota al pie que se puede pasar por alto.
    if kill_switch_invalidates_note:
        lines.append(kill_switch_invalidates_note)
        lines.append("")
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

    # NUEVO 2026-10-02 (ver docstring del modulo): distingue "sin entradas
    # porque el guard bloqueo" de "sin entradas por kill switch u otra
    # causa" - un OK de arriba, por si solo, no alcanza para probar que el
    # guard funciono.
    total_rejects = sum(reject_counts.values())
    lines.append("")
    if total_rejects:
        lines.append(
            f"ATENCION: {total_rejects} señal(es) de ENTRADA de weekly_asymmetric fueron "
            "RECHAZADAS ese mismo viernes por motivos AJENOS al weekend guard (el guard "
            "bloquea ANTES de generar la señal, nunca deja un REJECT - ver docstring del "
            "modulo):"
        )
        for bucket, count in sorted(reject_counts.items()):
            lines.append(f"  - {bucket}: {count}")
        lines.append(
            "Un RESULTADO OK de arriba, en presencia de estos rechazos, NO prueba que el "
            "weekend guard haya sido la causa de la ausencia de entradas - pudo ser "
            "cualquiera de estos otros motivos actuando primero, con o sin el guard."
        )
    elif not violations:
        lines.append(
            "Sin señales de ENTRADA rechazadas por otro motivo ese viernes (segun el Event "
            "Journal) - hasta donde este chequeo puede ver, el RESULTADO OK de arriba es "
            "evidencia real, no un silencio sin explicacion."
        )

    if kill_switch_state_available:
        if kill_switch_note:
            lines.append("")
            lines.append(kill_switch_note)
    else:
        lines.append("")
        lines.append(
            "NOTA: no se encontro el archivo de estado del kill switch en este entorno - no "
            "se pudo verificar si estuvo disparado ese viernes (correr este chequeo dentro "
            "del mismo contenedor/volumen de Northflank, o pasar --kill-switch-state, para "
            "incluir esa verificacion)."
        )

    if unknown_count:
        lines.append("")
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


def _load_kill_switch_state(path: Path) -> Optional[KillSwitchState]:
    """None si el archivo de estado no existe en este entorno (ej. corriendo
    el chequeo contra un CSV descargado, fuera del volumen de Northflank) -
    se distingue explicitamente de "no disparado" (ver format_report,
    `kill_switch_state_available`), nunca se asume lo segundo sin evidencia."""
    if not path.exists():
        return None
    return KillSwitch(path=path).status()


def run_check(
    position_events_csv: Optional[Path] = None,
    friday: Optional[date] = None,
    kill_switch_state_path: Optional[Path] = None,
) -> int:
    """
    Corre el chequeo completo e imprime el reporte. Devuelve el exit code
    (0 = OK, 1 = se encontraron violaciones) para uso en linea de comandos
    o encadenado en otro script - el exit code sigue midiendo SOLO
    violaciones confirmadas del guard (comportamiento preexistente, sin
    cambios); la distincion nueva de REJECT/kill switch es informativa, en
    el texto del reporte (ver docstring del modulo).
    """
    path = position_events_csv if position_events_csv is not None else POSITION_EVENTS_LOG
    target_friday = friday if friday is not None else most_recent_friday()
    ks_path = kill_switch_state_path if kill_switch_state_path is not None else KILL_SWITCH_STATE_FILE

    position_events_df = _load_position_events(path)
    violations = find_friday_entries_that_should_have_been_blocked(position_events_df, target_friday)
    unknown = entries_without_expiry_on_friday(position_events_df, target_friday)
    reject_df = find_entry_rejects_on_day(position_events_df, target_friday)
    reject_counts = summarize_entry_rejects(reject_df)
    ks_state = _load_kill_switch_state(ks_path)
    ks_note = kill_switch_status_note(target_friday, ks_state) if ks_state is not None else None
    ks_rejects_any_strategy = find_kill_switch_rejects_on_day_any_strategy(position_events_df, target_friday)
    ks_invalidates_note = kill_switch_invalidates_day_note(ks_rejects_any_strategy)

    print(format_report(
        violations, target_friday, len(unknown),
        reject_counts=reject_counts, kill_switch_note=ks_note,
        kill_switch_state_available=ks_state is not None,
        kill_switch_invalidates_note=ks_invalidates_note,
    ))
    return 1 if violations else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--position-events", default=str(POSITION_EVENTS_LOG), help="Ruta a position_events.csv")
    parser.add_argument("--friday", default=None, help="YYYY-MM-DD (ART). Default: el viernes mas reciente.")
    parser.add_argument(
        "--kill-switch-state", default=str(KILL_SWITCH_STATE_FILE),
        help="Ruta a kill_switch.json (default: state/kill_switch.json). Ausente = no disponible, nunca se asume 'no disparado'.",
    )
    args = parser.parse_args(argv)

    friday = date.fromisoformat(args.friday) if args.friday else None
    try:
        return run_check(Path(args.position_events), friday, Path(args.kill_switch_state))
    except WeekendGuardCheckUnavailable as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
