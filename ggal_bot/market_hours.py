"""
market_hours.py
==================
Unica fuente de verdad para "¿esta la rueda de BYMA abierta ahora?",
compartida por el BOT (ver run_bot.py, MEJORA 2026-10-01 - gate de
entradas/salidas/hedge fuera de horario) y el DASHBOARD (ver
dashboard/data/freshness.py, que antes tenia su propia copia de esta
misma logica - movida aca para que ambos consuman EXACTAMENTE la misma
definicion, en vez de arriesgarse a que diverjan con el tiempo).

SUPUESTO EXPLICITO, NO VERIFICADO (mismo criterio de honestidad que
ggal_bot/backtest/costs.py con el 0.20% de "derecho de mercado" BYMA -
una asuncion razonable pero marcada como tal, nunca escondida): no existe
en ningun otro lugar del codigo una constante ya establecida de horario
de rueda de BYMA. Se asume aca 11:00-17:00 ART (UTC-3 fijo, sin horario
de verano desde 2009 - mismo offset que
ggal_bot/config.py::eod_timezone_offset_hours), de lunes a viernes, SIN
calendario de feriados (un feriado de BYMA todavia se evaluaria como
"rueda abierta" con este modulo - no hay ninguna fuente de feriados
integrada). Si este supuesto es incorrecto, corregir
BYMA_SESSION_START_HOUR_ART / BYMA_SESSION_END_HOUR_ART aca abajo (un
solo lugar, usado tanto por el bot como por el dashboard).

MEJORA 2026-10-01 (mandato explicito del usuario, URGENTE - ver REPORT.md:
247 de 1183 fills en shadow_trades.csv con timestamp fuera de esta
ventana, incluyendo un caso concreto con ~ARS 420.000 de PnL fabricado en
23 minutos sobre GFGC6600OC): investigado y confirmado por codigo
(ggal_bot/data/live_shadow_feed.py:LiveShadowFeed) que fuera de horario
las fuentes reales (Data912RestSource/BrokerRestSource) devuelven
correctamente puntas vacias (no fabrican nada) - pero tras solo
`source_failure_threshold` (default 3) polls vacios consecutivos, el
failover automatico (diseñado como red de seguridad para una caida REAL
de la fuente durante la rueda) activa MockReplaySource, un generador 100%
sintetico de random walk que NO tiene ningun concepto de horario de rueda
y corre 24/7 con `as_of=time.time()` (siempre "fresco" para cualquier
guardia de staleness existente, que mide antiguedad del dato, no si el
mercado esta abierto). Resultado: el bot evaluaba entradas/salidas/hedge
toda la noche y los fines de semana contra precios fabricados. Este
modulo es el gate que corta eso en origen (ver run_bot.py:
GgalOptionsBot.recompute_cycle, RiskConfig.enforce_market_hours_gate).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

ART_OFFSET_HOURS = -3.0  # UTC-3 fijo (Argentina no tiene horario de verano desde 2009)
BYMA_SESSION_START_HOUR_ART = 11.0  # SUPUESTO no verificado, ver docstring del modulo
BYMA_SESSION_END_HOUR_ART = 17.0    # SUPUESTO no verificado, ver docstring del modulo


def _to_aware_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def is_within_byma_session(now_utc: Optional[datetime] = None) -> bool:
    """True si `now_utc` (o el momento actual si se omite) cae dentro de la
    rueda asumida (ver supuesto en el docstring del modulo), lunes a
    viernes. Acepta datetime naive (se asume UTC, nunca se adivina otra
    zona horaria) o aware."""
    now_utc = _to_aware_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    art = now_utc + timedelta(hours=ART_OFFSET_HOURS)
    if art.weekday() >= 5:  # 5=sabado, 6=domingo
        return False
    hour_frac = art.hour + art.minute / 60.0
    return BYMA_SESSION_START_HOUR_ART <= hour_frac < BYMA_SESSION_END_HOUR_ART
