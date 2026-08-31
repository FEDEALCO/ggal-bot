"""
http_utils.py
===============
Helper compartido para pegarle a APIs REST publicas (data912.com, usado por
`live_shadow_feed.py` para datos en vivo y por `technical_analysis.py` para
el historico de velas 1D) con un timeout de PARED REAL (wall-clock), no
solo el timeout que ofrece `requests`/`urllib3`.

Motivo (caso real reportado por el usuario, ver README, seccion "El bot
queda trabado varios minutos"): en Windows, una resolucion DNS colgada
(`getaddrinfo()`) puede bloquear la llamada de conexion mucho mas alla del
timeout configurado en `requests` - ese timeout se aplica sobre el socket
YA CREADO (fase de conexion/lectura), no necesariamente cubre la fase de
resolucion de nombre en si, que en algunos entornos Windows (VPN, DNS
corporativo, drivers de red particulares) puede quedar bloqueada varios
minutos pese a que el codigo pida `timeout=5.0`. El sintoma real observado
en el log del usuario: un `ConnectTimeoutError` con `timeout=5.0` en el
mensaje, que sin embargo tardo MINUTOS en aparecer - la excepcion en si es
correcta, pero tardo mucho mas que 5s en levantarse, y mientras tanto TODO
el ciclo de `run_bot.py` (recompute_cycle: IV/griegas, señales, riesgo,
hedge, vigilancia de ordenes, persistencia de estado) queda bloqueado
esperando esa unica llamada de red, ya que `Data912RestSource.fetch_snapshot()`
hace las llamadas de forma sincronica dentro del mismo hilo del loop
principal (ver run_bot.py, bucle en `main()`).

La solucion: correr la llamada de red en un thread de un pool COMPARTIDO
(un unico executor a nivel de modulo, reusado entre llamadas) y esperarla
con un timeout PROPIO via `concurrent.futures.Future.result(timeout=...)`,
que si corta de verdad al nivel del hilo que llama - el bot no sigue
esperando mas alla del limite configurado, sin importar cuanto tarde la
llamada bloqueada en resolverse por su cuenta a nivel de sistema operativo.
El hilo bloqueado sigue vivo en el pool hasta que el SO finalmente le
devuelva el control (no hay forma portable de matarlo a la fuerza en
Python puro), pero el bot ya no espera por el: el ciclo principal sigue
con el proximo poll enseguida, y la guardia de staleness de datos (ver
RiskConfig.max_market_data_staleness_seconds / run_bot._is_market_data_stale)
se encarga de pausar entradas nuevas si la falta de datos frescos se
sostiene en el tiempo.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("ggal_bot.http_utils")

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover - mismo degradado que el resto del proyecto
    requests = None
    _REQUESTS_AVAILABLE = False

# Pool COMPARTIDO a nivel de modulo (no uno por instancia/llamada): las
# llamadas de red de este proyecto son poco frecuentes (un par por poll de
# ~5s en Shadow, una por hora para el historico de Analisis Tecnico), asi
# que 4 workers alcanzan de sobra incluso si alguna llamada queda colgada
# por varios minutos - una nueva llamada solo esperaria por un worker libre
# si las 4 estuvieran colgadas EN SIMULTANEO, escenario extremo que de
# cualquier forma ya fallaria rapido por timeout (ver mas abajo) en vez de
# colgarse en la cola.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="ggal-bot-http")

# Margen sobre el timeout propio de `requests`: le da un poco de aire a la
# libreria para que levante su propia excepcion antes de forzar la nuestra
# (el caso normal, rapido), pero sin esperar mucho mas si el problema es,
# precisamente, que `requests` no esta respetando su propio timeout (el
# caso real que motiva este modulo).
_HARD_TIMEOUT_MARGIN_SECONDS = 2.0


def _run_with_hard_timeout(do_request, timeout: float, label: str) -> Any:
    """
    Nucleo compartido de timeout de pared real (ver docstring del modulo)
    entre `http_get_json()` y `http_request_json()`: corre `do_request` (sin
    argumentos) en el executor compartido y espera con `Future.result(timeout=...)`,
    que si corta de verdad a nivel del hilo que llama.
    """
    future = _EXECUTOR.submit(do_request)
    hard_timeout = timeout + _HARD_TIMEOUT_MARGIN_SECONDS
    try:
        return future.result(timeout=hard_timeout)
    except concurrent.futures.TimeoutError as exc:
        logger.warning(
            "timeout duro de %.1fs esperando %s - la llamada de red no respeto su propio timeout "
            "configurado (%.1fs); posible cuelgue de resolucion DNS/proxy a nivel de sistema "
            "operativo, no un problema de este codigo.",
            hard_timeout, label, timeout,
        )
        raise TimeoutError(
            f"Timeout duro de {hard_timeout:.1f}s esperando {label} (la llamada de red no respeto su "
            f"propio timeout configurado de {timeout:.1f}s)."
        ) from exc


def http_get_json(url: str, timeout: float, headers: Optional[Dict[str, str]] = None) -> Any:
    """
    GET + `.json()` con timeout de pared real (ver docstring del modulo).

    Lanza `TimeoutError` propio si el hard timeout se cumple - sea porque
    `requests` tardo de mas en levantar su propia excepcion de timeout, o
    por cualquier otro bloqueo a nivel de sistema operativo (DNS, proxy,
    VPN). Los llamadores de este helper (`Data912RestSource`,
    `Data912DailyBarsSource`, `BrokerRestSource` para GETs autenticados) ya
    envuelven sus llamadas en un `except Exception` generico (se degrada a
    Mock/Replay, NEUTRAL, o "fuente no disponible" segun el modulo), asi que
    no hace falta un manejo especial mas alla de este raise.

    `headers` es opcional (ej. `{"Authorization": "Bearer ..."}` para
    `BrokerRestSource`): sin headers, usa `requests.get()` tal cual lo hacia
    este modulo antes de agregar soporte de headers, para no cambiar el
    comportamiento (ni los mocks de test) del caso REST publico sin
    autenticacion (`Data912RestSource`/`Data912DailyBarsSource`).
    """
    if not _REQUESTS_AVAILABLE:
        raise RuntimeError("El paquete 'requests' no esta instalado.")

    def _do_request():
        if headers:
            response = requests.request("GET", url, timeout=timeout, headers=headers)
        else:
            response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()

    return _run_with_hard_timeout(_do_request, timeout, label=f"GET {url}")


def http_request_json(
    method: str, url: str, timeout: float,
    headers: Optional[Dict[str, str]] = None, data: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    Version generalizada de `http_get_json()`: mismo wrapper de timeout de
    pared real, pero permite cualquier metodo HTTP y un cuerpo de formulario
    (`data`, se envia como `application/x-www-form-urlencoded`, igual que
    `requests.post(..., data=payload)`). Se agrego para `BrokerRestSource`
    (ver live_shadow_feed.py): necesita un POST de login (usuario/contraseña)
    que `http_get_json()` (GET unicamente) no cubre. Ver su docstring para el
    detalle del mecanismo de timeout y del manejo de excepciones.
    """
    if not _REQUESTS_AVAILABLE:
        raise RuntimeError("El paquete 'requests' no esta instalado.")

    def _do_request():
        response = requests.request(method, url, timeout=timeout, headers=headers, data=data)
        response.raise_for_status()
        return response.json()

    return _run_with_hard_timeout(_do_request, timeout, label=f"{method} {url}")
