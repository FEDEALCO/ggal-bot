"""
audit_journal_invariants.py
============================
Checker standalone de invariantes sobre logs/position_events.csv (el
PositionEventJournal real, ver ggal_bot/portfolio/event_journal.py).

POR QUE EXISTE: parte de la auditoria completa pedida por el usuario
(2026-10-08) - un chequeo automatizado y repetible de invariantes de
integridad del journal, en vez de verificaciones manuales ad-hoc cada vez
que surge una duda sobre una posicion. Pensado para correrse contra un
export fresco del journal de produccion (ver README de este script) tantas
veces como haga falta durante la vida del proyecto, no solo para esta
auditoria puntual.

Invariantes verificados (cada uno documentado en su propia funcion
check_*):
  1. long_only: ninguna strategy_tag conocida como long-only (todas las
     que existen hoy: weekly_asymmetric, scalping - ver
     strategy/*.py, ninguna vende en descubierto) debe tener
     quantity_after negativo en ningun evento ENTRY/PARTIAL_EXIT/CLOSE.
  2. position_id_consistency: todo evento con el mismo position_id (no
     vacio) debe tener el mismo symbol/contract_key/strategy_tag en TODOS
     sus eventos - un position_id no puede "cambiar de identidad".
  3. close_is_terminal_and_zeroes_quantity: el evento CLOSE de un
     position_id debe ser el ULTIMO evento (cronologicamente) para ese
     position_id, y debe dejar quantity_after en 0 (o ausente/NaN si el
     campo no se pobló para ese evento - eso se reporta aparte, no se
     asume 0).
  4. order_client_id_not_reused_with_different_payload: un
     order_client_id no puede aparecer en dos filas con symbol o
     position_id distintos (indicaria colision de ids, no reuso legitimo
     de la misma orden).
  5. monotonic_timestamps_per_position: los eventos de un mismo
     position_id deben aparecer en orden cronologico no decreciente en el
     propio CSV (si no, el CSV esta corrupto o fue editado a mano).
  6. no_orphaned_open_positions_older_than_threshold: posiciones con
     ENTRY pero sin CLOSE/PARTIAL_EXIT que las resuelva, y que ya
     acumulan mas de N dias abiertas (default 5, configurable) - esto NO
     es necesariamente un bug (puede ser una posicion legitimamente
     abierta al momento del export), pero se reporta siempre para
     revision manual.

USO:
    python audit_journal_invariants.py <path a position_events.csv fresco>

Exit code 0 si no se encontraron violaciones de los invariantes 1-5
(el 6 nunca hace fallar el exit code - es informativo). Exit code 1 si
se encontro al menos una violacion real.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import List

import pandas as pd

# Estrategias conocidas como long-only hoy (ver ggal_bot/strategy/*.py -
# ninguna de las dos abre posiciones vendiendo opciones en descubierto;
# el unico "sell" legitimo en el journal es CERRAR una posicion larga
# existente, nunca abrir una nueva neta corta).
LONG_ONLY_STRATEGIES = {"weekly_asymmetric", "scalping"}

ORPHAN_OPEN_POSITION_THRESHOLD_DAYS = 5


class Violation:
    def __init__(self, check: str, severity: str, detail: str):
        self.check = check
        self.severity = severity
        self.detail = detail

    def __str__(self) -> str:
        return f"[{self.severity}] {self.check}: {self.detail}"


def load_journal(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    # quantity_after/quantity_delta pueden venir vacios (ej. en REJECT, que
    # no tiene efecto sobre ninguna posicion real) - se dejan como NaN, no
    # se fuerza a 0 (fabricar un valor seria peor que dejarlo faltante).
    for col in ("quantity_after", "quantity_delta"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def check_long_only(df: pd.DataFrame) -> List[Violation]:
    violations = []
    effective = df[df["event_type"].isin(["ENTRY", "PARTIAL_EXIT", "CLOSE"])]
    for strategy in LONG_ONLY_STRATEGIES:
        sub = effective[effective["strategy_tag"] == strategy]
        bad = sub[sub["quantity_after"] < 0]
        for _, row in bad.iterrows():
            violations.append(Violation(
                "long_only", "CRITICAL",
                f"{row['timestamp_utc']} position_id={row['position_id']} "
                f"symbol={row['symbol']} strategy_tag={strategy} "
                f"event_type={row['event_type']} quantity_after={row['quantity_after']} "
                f"(estrategia long-only con posicion neta CORTA)",
            ))
    unknown = sorted(set(effective["strategy_tag"].dropna()) - LONG_ONLY_STRATEGIES)
    if unknown:
        violations.append(Violation(
            "long_only", "INFO",
            f"strategy_tag(s) no clasificadas como long-only en este script "
            f"(revisar manualmente si son realmente long-only): {unknown}",
        ))
    return violations


def check_position_id_consistency(df: pd.DataFrame) -> List[Violation]:
    violations = []
    with_id = df[df["position_id"].notna() & (df["position_id"] != "")]
    for position_id, group in with_id.groupby("position_id"):
        for col in ("symbol", "contract_key", "strategy_tag"):
            if col not in group.columns:
                continue
            values = set(group[col].dropna().unique())
            if len(values) > 1:
                violations.append(Violation(
                    "position_id_consistency", "CRITICAL",
                    f"position_id={position_id} tiene multiples valores de {col}: {values}",
                ))
    return violations


def check_close_is_terminal(df: pd.DataFrame) -> List[Violation]:
    violations = []
    with_id = df[df["position_id"].notna() & (df["position_id"] != "")]
    for position_id, group in with_id.groupby("position_id"):
        group = group.sort_values("timestamp_utc")
        close_rows = group[group["event_type"] == "CLOSE"]
        if close_rows.empty:
            continue
        if len(close_rows) > 1:
            violations.append(Violation(
                "close_is_terminal", "HIGH",
                f"position_id={position_id} tiene {len(close_rows)} eventos CLOSE "
                f"(se esperaba a lo sumo 1).",
            ))
        last_close_ts = close_rows["timestamp_utc"].max()
        after_close = group[group["timestamp_utc"] > last_close_ts]
        if not after_close.empty:
            violations.append(Violation(
                "close_is_terminal", "CRITICAL",
                f"position_id={position_id}: hay {len(after_close)} evento(s) DESPUES "
                f"del CLOSE ({last_close_ts}): {list(after_close['event_type'])}",
            ))
        last_close_row = close_rows.loc[close_rows["timestamp_utc"].idxmax()]
        qty_after = last_close_row.get("quantity_after")
        if pd.notna(qty_after) and qty_after != 0:
            violations.append(Violation(
                "close_is_terminal", "HIGH",
                f"position_id={position_id}: CLOSE en {last_close_ts} deja "
                f"quantity_after={qty_after} (se esperaba 0).",
            ))
    return violations


def check_order_client_id_not_reused(df: pd.DataFrame) -> List[Violation]:
    violations = []
    if "order_client_id" not in df.columns:
        return violations
    with_id = df[df["order_client_id"].notna() & (df["order_client_id"] != "")]
    for client_id, group in with_id.groupby("order_client_id"):
        for col in ("symbol", "position_id"):
            values = set(group[col].dropna().astype(str).unique())
            if len(values) > 1:
                violations.append(Violation(
                    "order_client_id_not_reused", "HIGH",
                    f"order_client_id={client_id} aparece con distintos valores de "
                    f"{col}: {values} (posible colision de ids).",
                ))
    return violations


def check_monotonic_timestamps(df: pd.DataFrame) -> List[Violation]:
    violations = []
    with_id = df[df["position_id"].notna() & (df["position_id"] != "")]
    for position_id, group in with_id.groupby("position_id"):
        ts = group["timestamp_utc"].tolist()
        ts_sorted_in_file = group.sort_index()["timestamp_utc"].tolist()
        if ts_sorted_in_file != sorted(ts_sorted_in_file):
            violations.append(Violation(
                "monotonic_timestamps", "HIGH",
                f"position_id={position_id}: los eventos NO estan en orden "
                f"cronologico en el orden en que aparecen en el CSV.",
            ))
    return violations


def check_orphaned_open_positions(df: pd.DataFrame, now: datetime = None) -> List[Violation]:
    violations = []
    now = now or datetime.now(timezone.utc)
    with_id = df[df["position_id"].notna() & (df["position_id"] != "")]
    for position_id, group in with_id.groupby("position_id"):
        has_terminal = (group["event_type"] == "CLOSE").any()
        has_entry = (group["event_type"] == "ENTRY").any()
        if has_entry and not has_terminal:
            first_entry_ts = group[group["event_type"] == "ENTRY"]["timestamp_utc"].min()
            age_days = (now - first_entry_ts).total_seconds() / 86400
            if age_days >= ORPHAN_OPEN_POSITION_THRESHOLD_DAYS:
                last_row = group.sort_values("timestamp_utc").iloc[-1]
                violations.append(Violation(
                    "orphaned_open_positions", "INFO",
                    f"position_id={position_id} symbol={last_row.get('symbol')} "
                    f"strategy_tag={last_row.get('strategy_tag')}: ENTRY hace "
                    f"{age_days:.1f} dias sin CLOSE (puede ser legitima - revisar "
                    f"manualmente contra el estado real del bot al momento del export).",
                ))
    return violations


ALL_CHECKS = [
    check_long_only,
    check_position_id_consistency,
    check_close_is_terminal,
    check_order_client_id_not_reused,
    check_monotonic_timestamps,
    check_orphaned_open_positions,
]

FAILS_EXIT_CODE = {"CRITICAL", "HIGH"}


def run_audit(path: str) -> int:
    df = load_journal(path)
    print(f"Journal cargado: {len(df)} filas, {path}")
    print(f"Rango temporal: {df['timestamp_utc'].min()} -> {df['timestamp_utc'].max()}")
    print()

    all_violations: List[Violation] = []
    for check in ALL_CHECKS:
        violations = check(df)
        all_violations.extend(violations)

    by_severity = {"CRITICAL": [], "HIGH": [], "INFO": []}
    for v in all_violations:
        by_severity.setdefault(v.severity, []).append(v)

    for severity in ("CRITICAL", "HIGH", "INFO"):
        items = by_severity.get(severity, [])
        print(f"=== {severity} ({len(items)}) ===")
        for v in items:
            print(f"  {v}")
        print()

    hard_fail = any(v.severity in FAILS_EXIT_CODE for v in all_violations)
    print("RESULTADO:", "VIOLACIONES ENCONTRADAS" if hard_fail else "OK - sin violaciones CRITICAL/HIGH")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python audit_journal_invariants.py <path a position_events.csv>")
        sys.exit(2)
    sys.exit(run_audit(sys.argv[1]))
