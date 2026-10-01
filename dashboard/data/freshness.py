"""
dashboard/data/freshness.py
=============================
Frescura de datos (Fase 1, mandato explicito del usuario): timestamp del
ultimo evento de cada fuente y alerta si esta desactualizado, "por
ejemplo, mas de N minutos en horario de rueda".

MEJORA 2026-10-01: la logica de "¿esta la rueda de BYMA abierta ahora?"
(incluido el supuesto explicito y no verificado de 11:00-17:00 ART) se
movio a ggal_bot/market_hours.py, que ahora es la UNICA fuente de verdad -
la usa tambien run_bot.py (RiskConfig.enforce_market_hours_gate) para
dejar de evaluar entradas/salidas/hedge fuera de horario (ver docstring de
ese modulo para el bug real que esto corrigio). Este archivo re-exporta
los mismos nombres para no romper a nadie que ya importaba
dashboard.data.freshness.is_within_byma_session /
BYMA_SESSION_START_HOUR_ART / BYMA_SESSION_END_HOUR_ART.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ggal_bot.market_hours import (  # noqa: F401 - re-exportado por compatibilidad
    ART_OFFSET_HOURS,
    BYMA_SESSION_END_HOUR_ART,
    BYMA_SESSION_START_HOUR_ART,
    is_within_byma_session,
)


@dataclass
class SourceFreshness:
    source_name: str
    last_event_utc: Optional[datetime]
    minutes_since: Optional[float]
    has_data: bool
    is_stale: bool
    reason: Optional[str]  # motivo de "SIN DATOS" o de "desactualizado", para el tooltip


def _to_aware_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def compute_freshness(
    source_name: str,
    last_event_utc: Optional[datetime],
    *,
    now_utc: Optional[datetime] = None,
    stale_after_minutes: float = 15.0,
    only_flag_stale_during_session: bool = True,
    no_data_reason: Optional[str] = None,
) -> SourceFreshness:
    """
    `last_event_utc=None` -> SIN DATOS explicito (nunca se fabrica un
    timestamp). `only_flag_stale_during_session=True` (default): fuera de
    la rueda asumida, un dato viejo NO se marca stale (el bot no opera
    fuera de horario, es esperable que no haya eventos nuevos) - pasar
    False para paneles que si deben alertar fuera de rueda.
    """
    now_utc = _to_aware_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)

    if last_event_utc is None:
        return SourceFreshness(
            source_name=source_name, last_event_utc=None, minutes_since=None,
            has_data=False, is_stale=False,
            reason=no_data_reason or "SIN DATOS: esta fuente todavia no tiene ningun evento registrado.",
        )

    last_event_utc = _to_aware_utc(last_event_utc)
    minutes_since = (now_utc - last_event_utc).total_seconds() / 60.0
    session_gate_ok = (not only_flag_stale_during_session) or is_within_byma_session(now_utc)
    is_stale = session_gate_ok and minutes_since > stale_after_minutes

    reason = None
    if is_stale:
        reason = (
            f"Ultimo evento hace {minutes_since:.0f} min "
            f"(umbral {stale_after_minutes:.0f} min en horario de rueda)."
        )

    return SourceFreshness(
        source_name=source_name, last_event_utc=last_event_utc, minutes_since=minutes_since,
        has_data=True, is_stale=is_stale, reason=reason,
    )
