"""
market_hours.py
==================
Unica fuente de verdad para "¿esta la rueda de BYMA abierta ahora?",
compartida por el BOT (ver run_bot.py, MEJORA 2026-10-01 - gate de
entradas/salidas/hedge fuera de horario) y el DASHBOARD (ver
dashboard/data/freshness.py, que antes tenia su propia copia de esta
misma logica - movida aca para que ambos consuman EXACTAMENTE la misma
definicion, en vez de arriesgarse a que diverjan con el tiempo).

VERIFICADO 2026-10-02 (Tarea #27/#28 item 6, a pedido explicito del
usuario: "Verificar el horario de rueda de opciones BYMA contra una
fuente oficial" - reemplaza el supuesto no verificado anterior de
11:00-17:00 ART). Fuentes primarias, confirmadas por WebFetch directo
(no solo por el reporte de un subagente de investigacion):
  - BYMA, Comunicado/Circular N. 19024 (PDF oficial, verificado
    verbatim): "Negociacion Regular - Opciones y Plazos 10:30 hs. a
    18:00 hs." y "Ejercicios y Vto. Ejercicio de Opciones 10:30 hs. a
    15:59 hs.", con vigencia explicita "a partir del 02 de Noviembre de
    2026". URL: https://cdn.prod.website-files.com/6697a441a50c6b926e1972e0/6ab2dda27ea9e2c80e659a15_BYMA-COM19024-Horarios_de_Negociacion_Liquidacion_Recepcion-v2.pdf
  - Bloomberg Linea (corrobora el cambio de apertura ya vigente desde
    antes, verificado verbatim): "desde el lunes 28 de julio, el
    horario de operacion en los segmentos... opciones... comenzara a
    las 10:30 AM (hoy es a las 11:00 AM) y finalizara a las 5:00 PM".

Esto confirma DOS cosas distintas, con vigencias distintas:
  (a) La APERTURA ya es 10:30 ART (no 11:00) desde el 28/jul/2025 -
      BYMA_SESSION_START_HOUR_ART se corrige a 10.5 sin condicion de
      fecha, porque ya esta en vigencia HOY.
  (b) El CIERRE es 17:00 ART HOY (2026-10-02), pero cambia a 18:00 ART
      a partir del 02/nov/2026 por el mismo comunicado oficial citado
      arriba - una fecha futura concreta y ya confirmada, no una
      especulacion. Por eso el cierre NO es una constante fija: ver
      `_session_end_hour_art(art_date)` mas abajo, que selecciona 17.0 u
      18.0 segun la fecha (en ART) del `now_utc` evaluado. Si este
      comunicado llegara a posponerse o revertirse, corregir
      `_BYMA_SESSION_END_CHANGE_DATE_ART` / los dos valores de cierre
      aca abajo (un solo lugar, usado tanto por el bot como por el
      dashboard).

Sigue sin existir calendario de feriados (un feriado de BYMA todavia se
evaluaria como "rueda abierta" con este modulo - no hay ninguna fuente
de feriados integrada); eso continua siendo un gap conocido, no cubierto
por esta verificacion.

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

from datetime import date, datetime, timedelta, timezone
from typing import Optional

ART_OFFSET_HOURS = -3.0  # UTC-3 fijo (Argentina no tiene horario de verano desde 2009)
BYMA_SESSION_START_HOUR_ART = 10.5  # VERIFICADO (10:30 ART, vigente desde 2025-07-28), ver docstring del modulo

# Cierre: VERIFICADO con vigencias distintas (ver docstring del modulo,
# Comunicado BYMA N. 19024). BYMA_SESSION_END_HOUR_ART se deja como el
# valor VIGENTE HOY (2026-10-02 en adelante hasta el cambio) solo por
# compatibilidad con codigo que ya importaba esta constante directamente
# (ej. dashboard/data/freshness.py, re-exportada ahi) - is_within_byma_session
# de aca abajo NO la usa directamente, usa _session_end_hour_art(), que es
# fecha-consciente y la unica fuente de verdad real.
BYMA_SESSION_END_HOUR_ART = 17.0
BYMA_SESSION_END_HOUR_ART_FROM_2026_11_02 = 18.0
_BYMA_SESSION_END_CHANGE_DATE_ART = date(2026, 11, 2)  # vigencia oficial confirmada, Comunicado BYMA N. 19024


def _to_aware_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _session_end_hour_art(art_date: date) -> float:
    """Hora de cierre (en horas ART, fraccionaria) vigente para `art_date`
    (la fecha calendario YA convertida a ART, no UTC) - ver docstring del
    modulo para las dos vigencias confirmadas oficialmente."""
    if art_date >= _BYMA_SESSION_END_CHANGE_DATE_ART:
        return BYMA_SESSION_END_HOUR_ART_FROM_2026_11_02
    return BYMA_SESSION_END_HOUR_ART


def is_within_byma_session(now_utc: Optional[datetime] = None) -> bool:
    """True si `now_utc` (o el momento actual si se omite) cae dentro de la
    rueda verificada (ver docstring del modulo), lunes a viernes. Acepta
    datetime naive (se asume UTC, nunca se adivina otra zona horaria) o
    aware. El cierre es fecha-consciente (17:00 ART hasta 2026-11-01
    inclusive, 18:00 ART desde 2026-11-02 en adelante)."""
    now_utc = _to_aware_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    art = now_utc + timedelta(hours=ART_OFFSET_HOURS)
    if art.weekday() >= 5:  # 5=sabado, 6=domingo
        return False
    hour_frac = art.hour + art.minute / 60.0
    end_hour = _session_end_hour_art(art.date())
    return BYMA_SESSION_START_HOUR_ART <= hour_frac < end_hour
