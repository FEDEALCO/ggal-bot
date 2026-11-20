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

RE-VERIFICADO 2026-11-18/2026-11-20 (a pedido explicito del usuario, caso
real GFGV4000DI - ver RESEARCH.md, seguimiento 18/11 3ra parte): el
Comunicado BYMA N. 19024 de arriba quedo SUPERADO por un comunicado mas
nuevo, N. 19031 (28/10/2026, PDF oficial leido completo via WebFetch
directo, verificado verbatim): "Negociacion Regular - Opciones y Plazos
10:30 hs. a 18:00 hs.", vigente "a partir del 02/11/2026", y DEJA SIN
EFECTO explicitamente al N. 19024 - pero con los MISMOS valores exactos
(10:30 apertura, 18:00 cierre). URL:
https://cdn.prod.website-files.com/6697a441a50c6b926e1972e0/6ae39100dbd9381f5489d706_BYMA-COM19031-Horarios_Negociacion_liquidacion_recepcion_de_informacion.pdf
Un articulo de Ambito (23/09/2026) corrobora que este corrimiento de
horario de BYMA esta atado al cambio estacional de horario de EE.UU.
("para alinearse con Wall Street"). Esta re-verificacion NO cambio
ningun valor (19031 confirma los mismos numeros que 19024), pero motivo
el rediseño de abajo: en vez de UNA sola fecha de corte hardcodeada,
ahora hay una TABLA de vigencias (`BYMA_OPTIONS_SESSION_REGIMES`), cada
una con su propia fecha de inicio y cita de fuente - para que un futuro
cambio de horario (y el comunicado que eventualmente lo reemplace a
19031, como 19031 reemplazo a 19024) solo requiera AGREGAR una fila
nueva a la tabla, nunca reemplazar valores existentes ni arriesgarse a
perder la trazabilidad de que fuente respalda cada vigencia pasada.

Esto confirma DOS cosas distintas, con vigencias distintas:
  (a) La APERTURA ya es 10:30 ART (no 11:00) desde el 28/jul/2025 - sin
      condicion de fecha adicional en ninguna vigencia conocida hasta
      ahora (nunca volvio a cambiar).
  (b) El CIERRE fue 17:00 ART desde el 28/jul/2025, y cambia a 18:00 ART
      a partir del 02/nov/2026 (confirmado por 19024, re-confirmado por
      19031) - una fecha concreta, no una especulacion.

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

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Set, Tuple

logger = logging.getLogger(__name__)

ART_OFFSET_HOURS = -3.0  # UTC-3 fijo (Argentina no tiene horario de verano desde 2009)


@dataclass(frozen=True)
class SessionRegime:
    """Una vigencia confirmada del horario de opciones de BYMA: desde
    `effective_from` (fecha calendario ART, inclusive) hasta la proxima
    vigencia de la tabla (o indefinidamente si es la ultima), rige
    `open_hour_art`/`close_hour_art` (horas ART, fraccionarias) - ver
    `source` para la cita exacta y verificable de donde sale cada una."""
    effective_from: date
    open_hour_art: float
    close_hour_art: float
    source: str


# Tabla de vigencias (REDISEÑO 2026-11-20, a pedido explicito del usuario
# tras el caso real GFGV4000DI - ver docstring del modulo): reemplaza la
# constante de corte unica que existia antes
# (`_BYMA_SESSION_END_CHANGE_DATE_ART`/`BYMA_SESSION_END_HOUR_ART_FROM_2026_11_02`).
# ORDEN: ascendente por `effective_from` - `_regime_for()` abajo asume esto
# para encontrar la vigencia MAS RECIENTE que ya empezo para una fecha
# dada. Un cambio de horario futuro (el comunicado que eventualmente
# reemplace a 19031, igual que 19031 reemplazo a 19024) se incorpora
# AGREGANDO una fila nueva al final, nunca editando ni borrando las
# anteriores - preserva la trazabilidad de que vigencia rigio cada fecha
# historica ya operada.
BYMA_OPTIONS_SESSION_REGIMES: Tuple[SessionRegime, ...] = (
    SessionRegime(
        effective_from=date(2025, 7, 28),
        open_hour_art=10.5,
        close_hour_art=17.0,
        source=(
            "Bloomberg Linea (verificado verbatim 2026-10-02): 'desde el "
            "lunes 28 de julio, el horario de operacion en los "
            "segmentos... opciones... comenzara a las 10:30 AM (hoy es a "
            "las 11:00 AM) y finalizara a las 5:00 PM'."
        ),
    ),
    SessionRegime(
        effective_from=date(2026, 11, 2),
        open_hour_art=10.5,
        close_hour_art=18.0,
        source=(
            "BYMA, Comunicado N. 19031 (28/10/2026, PDF oficial leido "
            "completo via WebFetch, verificado verbatim 2026-11-18/20): "
            "'Negociacion Regular - Opciones y Plazos 10:30 hs. a 18:00 "
            "hs.', vigente desde 02/11/2026 - deja sin efecto (pero "
            "confirma los mismos valores que) el Comunicado N. 19024 "
            "(verificado originalmente 2026-10-02). URL 19031: "
            "https://cdn.prod.website-files.com/6697a441a50c6b926e1972e0/"
            "6ae39100dbd9381f5489d706_BYMA-COM19031-Horarios_Negociacion_"
            "liquidacion_recepcion_de_informacion.pdf . Corroborado por "
            "Ambito (23/09/2026): este cambio esta atado al corrimiento "
            "estacional de horario de EE.UU. ('para alinearse con Wall "
            "Street')."
        ),
    ),
)

# Compatibilidad hacia atras (dashboard/data/freshness.py re-exporta estos
# dos nombres - ver su propio docstring): snapshot de la vigencia MAS
# RECIENTE de la tabla al momento de importar este modulo, nunca
# reevaluado por fecha. NINGUNA funcion de este modulo los usa
# internamente (is_within_byma_session/_session_end_hour_art siempre
# resuelven contra BYMA_OPTIONS_SESSION_REGIMES, fecha-conscientes) - solo
# existen para no romper a quien ya los importaba directamente.
BYMA_SESSION_START_HOUR_ART = BYMA_OPTIONS_SESSION_REGIMES[-1].open_hour_art
BYMA_SESSION_END_HOUR_ART = BYMA_OPTIONS_SESSION_REGIMES[-1].close_hour_art

# Dedupe de la alerta de "fecha sin vigencia conocida" (ver _regime_for):
# una sola vez por fecha ART distinta, no en cada ciclo de poll (cada
# ~5s via GGAL_BOT_SHADOW_POLL_SECONDS - sin este dedupe, inundaria el
# log exactamente igual que el hallazgo real de GFGV4000DI que motivo el
# throttle de quote_gap_backstop el 18/11, ver RESEARCH.md).
_uncovered_dates_logged: Set[date] = set()


def _regime_for(art_date_value: date) -> SessionRegime:
    """Vigencia aplicable a `art_date_value` (fecha calendario ART): la de
    `effective_from` mas reciente que ya haya empezado (<=). Si
    `art_date_value` es ANTERIOR a la vigencia mas antigua conocida (el
    caso de "fecha actual no cubierta por ninguna vigencia" que pidio el
    usuario cubrir) - solo puede pasar para fechas antes del 2025-07-28,
    ya que la ULTIMA vigencia de la tabla no tiene fecha de fin y cubre
    cualquier fecha futura hasta que se agregue una vigencia mas nueva -
    se alerta (una vez por fecha, ver _uncovered_dates_logged) y se cae a
    la vigencia mas antigua conocida como mejor aproximacion disponible,
    en vez de fallar o adivinar un valor no verificado."""
    applicable = [r for r in BYMA_OPTIONS_SESSION_REGIMES if r.effective_from <= art_date_value]
    if not applicable:
        oldest = BYMA_OPTIONS_SESSION_REGIMES[0]
        if art_date_value not in _uncovered_dates_logged:
            logger.warning(
                "market_hours: %s es anterior a la vigencia mas antigua conocida "
                "(%s, vigente desde %s) - ninguna fuente oficial verificada cubre "
                "esta fecha. Se usa esa vigencia como fallback conservador "
                "(apertura=%.2f, cierre=%.2f ART); revisar "
                "BYMA_OPTIONS_SESSION_REGIMES si esto es inesperado en produccion.",
                art_date_value, oldest.source, oldest.effective_from,
                oldest.open_hour_art, oldest.close_hour_art,
            )
            _uncovered_dates_logged.add(art_date_value)
        return oldest
    return max(applicable, key=lambda r: r.effective_from)


def _to_aware_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _session_end_hour_art(art_date: date) -> float:
    """Hora de cierre (en horas ART, fraccionaria) vigente para `art_date`
    (la fecha calendario YA convertida a ART, no UTC) - ver
    BYMA_OPTIONS_SESSION_REGIMES/_regime_for arriba para la tabla completa
    de vigencias confirmadas oficialmente."""
    return _regime_for(art_date).close_hour_art


def _session_open_hour_art(art_date: date) -> float:
    """Hora de apertura (en horas ART, fraccionaria) vigente para
    `art_date` - ver _session_end_hour_art arriba. Separada por simetria
    (hasta ahora la apertura nunca cambio de vigencia, pero la tabla ya
    soporta que lo haga sin tocar ninguna funcion de este modulo)."""
    return _regime_for(art_date).open_hour_art


def is_within_byma_session(now_utc: Optional[datetime] = None) -> bool:
    """True si `now_utc` (o el momento actual si se omite) cae dentro de la
    rueda verificada (ver docstring del modulo), lunes a viernes. Acepta
    datetime naive (se asume UTC, nunca se adivina otra zona horaria) o
    aware. Apertura y cierre son fecha-conscientes via
    BYMA_OPTIONS_SESSION_REGIMES/_regime_for (17:00 ART hasta 2026-11-01
    inclusive, 18:00 ART desde 2026-11-02 en adelante, con alerta si
    alguna fecha evaluada no esta cubierta por ninguna vigencia)."""
    now_utc = _to_aware_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    art = now_utc + timedelta(hours=ART_OFFSET_HOURS)
    if art.weekday() >= 5:  # 5=sabado, 6=domingo
        return False
    hour_frac = art.hour + art.minute / 60.0
    regime = _regime_for(art.date())
    return regime.open_hour_art <= hour_frac < regime.close_hour_art


def art_date(now_utc: Optional[datetime] = None) -> date:
    """Fecha calendario en ART (ver ART_OFFSET_HOURS arriba) de `now_utc`
    (o el momento actual si se omite). Unica fuente de verdad para "que
    dia es HOY en ART" - ver portfolio/expiry_handler.py, que la usa para
    decidir si una opcion ya vencio (nunca convierte UTC->ART con su
    propia aritmetica, para no divergir de este modulo)."""
    now_utc = _to_aware_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    return (now_utc + timedelta(hours=ART_OFFSET_HOURS)).date()


def is_after_session_close(now_utc: Optional[datetime] = None) -> bool:
    """True si `now_utc` (o el momento actual) ya cruzo la hora de cierre
    de la rueda BYMA de SU PROPIA fecha calendario ART - a diferencia de
    is_within_byma_session() (que tambien es False antes de la apertura o
    en fin de semana, porque responde "esta la rueda abierta ahora"), esta
    funcion responde unicamente "ya paso el cierre de hoy" (True tambien
    en fin de semana / fuera de dias habiles, porque el cierre del ultimo
    dia habil ya quedo atras). Pensada para portfolio/expiry_handler.py:
    una opcion que vence HOY se cierra recien cuando esto es True, nunca
    antes de que la rueda de hoy termine."""
    now_utc = _to_aware_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    art = now_utc + timedelta(hours=ART_OFFSET_HOURS)
    hour_frac = art.hour + art.minute / 60.0
    return hour_frac >= _session_end_hour_art(art.date())
