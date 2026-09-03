"""
fetch_bot_logs.py
==================
Descarga los logs del servicio de Northflank (bot + dashboard, ver
entrypoint.sh) de las ultimas N horas (default 48) usando la API REST de
Northflank, y los guarda en un archivo .log local - para no tener que
scrollear/copiar a mano desde la UI de Northflank cada vez.

Requiere 3 variables de entorno (podes ponerlas en el .env del proyecto,
el mismo que ya usa GGAL_BOT_* / PYROFEX_*, o exportarlas antes de correr
el script):

    NORTHFLANK_API_TOKEN    Personal API token de Northflank.
                            Se genera en: tu cuenta de Northflank ->
                            configuracion de cuenta (icono de perfil,
                            arriba a la derecha) -> "API tokens" ->
                            "Create token". Guardalo, no se vuelve a
                            mostrar despues.

    NORTHFLANK_PROJECT_ID   ID del proyecto (no el nombre visible).
                            Se ve en la UI: abri el proyecto -> menu de
                            opciones (···) en el header -> "View
                            specification" -> campo "id" (o "project"
                            arriba del todo de la URL del navegador,
                            justo despues de /projects/).

    NORTHFLANK_SERVICE_ID   ID del servicio del bot (no el nombre
                            visible). Mismo mecanismo: abri el servicio
                            -> menu de opciones (···) -> "View
                            specification" -> campo "id".

Uso:
    python fetch_bot_logs.py                     # ultimas 48hs, log runtime
    python fetch_bot_logs.py --hours 12          # ultimas 12hs
    python fetch_bot_logs.py --output mis_logs.log
    python fetch_bot_logs.py --grep "ERROR|WARNING"   # solo lineas que matcheen ese regex

Nota: el contenedor corre el bot Y el dashboard juntos (ver
entrypoint.sh) - ambos escriben a la misma salida estandar del
contenedor, asi que este script trae las dos cosas mezcladas por
timestamp (igual que ves hoy en la UI de Northflank).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = Path(__file__).resolve().parent
    _env_path = _PROJECT_ROOT / ".env"
    load_dotenv(dotenv_path=_env_path if _env_path.exists() else None)
except ImportError:
    pass  # python-dotenv es opcional; si no esta, se usan las env vars ya seteadas

try:
    import requests
except ImportError:
    print("Falta 'requests'. Instalalo con: pip install requests", file=sys.stderr)
    sys.exit(1)


# Override solo para tests locales (ver validation/); en uso normal esto
# siempre apunta a la API real de Northflank.
API_BASE = os.getenv("NORTHFLANK_API_BASE", "https://api.northflank.com/v1")


def _iso(dt: datetime) -> str:
    """
    BUG REAL CORREGIDO (reportado por el usuario: la API de Northflank
    devolvia 400 Bad Request): la version anterior armaba el timestamp
    recortando los ultimos 3 caracteres del string ya formateado
    (`strftime("...%f")[: -3]`), asumiendo que eso recortaba de
    microsegundos (6 digitos) a milisegundos (3 digitos) - pero recortar
    CARACTERES del string no es lo mismo que recortar DIGITOS: dejaba 4
    digitos de fraccion de segundo en vez de 3 (ej. "...38.1252Z" en vez
    de "...38.125Z"), un formato que la API rechazaba. Se arma la fraccion
    de milisegundos explicitamente a partir de dt.microsecond en vez de
    recortar el string.
    """
    millis = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def _parse_ts(ts: str) -> datetime:
    # Northflank devuelve ISO8601 con 'Z'; distintas cantidades de decimales
    # de segundo segun la fuente del log, asi que se prueba con y sin
    # microsegundos en vez de asumir un formato fijo.
    ts = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        # Sin fraccion de segundos.
        return datetime.fromisoformat(ts.split(".")[0] + "+00:00")


MAX_RETRIES_PER_PAGE = 5
REQUEST_TIMEOUT_SECONDS = 60  # ver _request_page_with_retries(): antes 30s, insuficiente en la practica


def _request_page_with_retries(url: str, headers: dict, params: dict) -> "requests.Response":
    """
    BUG REAL CORREGIDO (reportado por el usuario: ReadTimeoutError a mitad
    de una descarga larga - ya habia traido 49 paginas/49.000 lineas
    exitosamente antes de que UNA sola pagina tardara mas de 30s en
    responder y tirara abajo TODO el script sin guardar nada de lo ya
    traido). Se reintenta cada pagina individualmente con backoff
    exponencial ante timeouts/errores de conexion (no ante errores 4xx de
    la API en si, esos no se solucionan reintentando) antes de darse por
    vencido. El timeout por request tambien subio de 30s a 60s: una
    ventana de 48hs con miles de lineas puede tardar mas en armarse del
    lado de Northflank de lo que tarda una consulta chica.
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES_PER_PAGE + 1):
        try:
            return requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt == MAX_RETRIES_PER_PAGE:
                break
            wait_s = min(2 ** attempt, 30)
            print(
                f"  fallo de red ({exc.__class__.__name__}) en el intento {attempt}/{MAX_RETRIES_PER_PAGE} - "
                f"reintentando en {wait_s}s...", file=sys.stderr,
            )
            time.sleep(wait_s)
    assert last_exc is not None
    raise last_exc


def iter_log_pages(
    token: str, project_id: str, service_id: str,
    start: datetime, end: datetime, log_type: str = "runtime",
    page_line_limit: int = 1000,  # tope maximo real de la API (ver main(): 2000 daba 400)
):
    """
    Generador que trae los logs entre start y end, paginando hacia adelante
    (la API de Northflank no documenta un cursor - se pagina avanzando el
    startTime al timestamp del ultimo log recibido en cada tanda, hasta
    que una tanda vuelve vacia o mas corta que el limite de pagina), y
    entrega (yield) cada tanda apenas llega - para que quien lo consuma
    (ver main()) pueda ir escribiendo a disco de forma incremental en vez
    de acumular todo en memoria hasta el final.
    """
    url = f"{API_BASE}/projects/{project_id}/services/{service_id}/logs"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    cursor = start
    page = 0
    while cursor < end:
        page += 1
        params = {
            "type": log_type,
            "queryType": "range",
            "startTime": _iso(cursor),
            "endTime": _iso(end),
            "direction": "forward",
            "lineLimit": page_line_limit,
        }
        resp = _request_page_with_retries(url, headers, params)
        if resp.status_code == 401:
            print(
                "Error 401 (no autorizado): el NORTHFLANK_API_TOKEN es invalido, expiro, o no "
                "tiene permiso 'View Observability' sobre este servicio.", file=sys.stderr,
            )
            sys.exit(1)
        if resp.status_code == 404:
            print(
                "Error 404 (no encontrado): revisa NORTHFLANK_PROJECT_ID / NORTHFLANK_SERVICE_ID - "
                "tienen que ser los ID internos (ver 'View specification' en la UI), no los nombres "
                "visibles.", file=sys.stderr,
            )
            sys.exit(1)
        if resp.status_code == 429:
            wait_s = float(resp.headers.get("x-ratelimit-reset", "5"))
            print(f"Rate limit alcanzado, esperando {wait_s:.0f}s...", file=sys.stderr)
            time.sleep(wait_s)
            continue
        if resp.status_code >= 400:
            # Se imprime el cuerpo de la respuesta ANTES de levantar la
            # excepcion: Northflank suele devolver el motivo exacto del
            # rechazo (ej. un parametro con formato invalido) en el body,
            # asi que esto deja el error autodiagnosticable sin tener que
            # ir y venir para pedir mas detalle.
            print(f"Error {resp.status_code} de la API - cuerpo de la respuesta:\n{resp.text}", file=sys.stderr)
        resp.raise_for_status()

        batch = resp.json().get("data", [])
        print(f"  pagina {page}: {len(batch)} lineas (desde {_iso(cursor)})", file=sys.stderr)
        if not batch:
            break

        yield batch

        last_ts = _parse_ts(batch[-1]["ts"])
        next_cursor = last_ts + timedelta(milliseconds=1)
        if next_cursor <= cursor:
            # Salvaguarda anti-loop-infinito: si el cursor no avanza (todas
            # las lineas de esta tanda comparten el mismo timestamp exacto),
            # se corta aca en vez de pedir la misma pagina para siempre.
            break
        cursor = next_cursor

        if len(batch) < page_line_limit:
            break  # ultima tanda: vino mas corta que el limite de pagina

        time.sleep(0.15)  # cortesia con el rate limit (1000 req/hora)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("Uso:")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=48.0, help="Ventana hacia atras en horas (default: 48).")
    parser.add_argument("--type", default="runtime", help="Tipo de log de Northflank (default: runtime). Otros: build, deploy, ingress, mesh, cdn, backup, restore.")
    parser.add_argument("--output", default=None, help="Archivo de salida (default: auto-generado con timestamp).")
    parser.add_argument("--grep", default=None, help="Solo guarda lineas cuyo texto matchee este regex (ej. 'ERROR|WARNING').")
    parser.add_argument(
        "--line-limit", type=int, default=1000,
        help="Lineas por pagina de la API (default y tope maximo real de la API: 1000).",
    )
    args = parser.parse_args()

    # BUG REAL CORREGIDO (reportado por el usuario: la API de Northflank
    # devolvia 400 - "lineLimit must be less than or equal to 1000"): el
    # default anterior (2000) ya superaba el tope real de la API, y un
    # --line-limit pasado a mano tambien podia superarlo. Se corrige el
    # default a 1000 y se aclara ademas con un tope defensivo aca, para que
    # un valor invalido nunca llegue a pisar el error 400 de nuevo.
    if args.line_limit > 1000:
        print(
            f"--line-limit={args.line_limit} supera el maximo real de la API de Northflank (1000) - "
            "se usa 1000.", file=sys.stderr,
        )
        args.line_limit = 1000

    token = os.getenv("NORTHFLANK_API_TOKEN", "")
    project_id = os.getenv("NORTHFLANK_PROJECT_ID", "")
    service_id = os.getenv("NORTHFLANK_SERVICE_ID", "")
    missing = [
        name for name, val in (
            ("NORTHFLANK_API_TOKEN", token),
            ("NORTHFLANK_PROJECT_ID", project_id),
            ("NORTHFLANK_SERVICE_ID", service_id),
        ) if not val.strip()
    ]
    if missing:
        print(
            "Faltan variables de entorno: " + ", ".join(missing) + "\n"
            "Ver el docstring de este archivo (arriba) para como conseguir cada una - "
            "podes agregarlas al .env del proyecto (mismo archivo que PYROFEX_USER, etc.) o "
            "exportarlas antes de correr el script.", file=sys.stderr,
        )
        sys.exit(1)

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)

    grep_pattern = None
    if args.grep:
        import re
        grep_pattern = re.compile(args.grep)

    output_path = Path(args.output) if args.output else Path(
        f"northflank_logs_{args.hours:g}h_{end.strftime('%Y%m%dT%H%M%SZ')}.log"
    )

    print(f"Trayendo logs tipo='{args.type}' desde {_iso(start)} hasta {_iso(end)}...", file=sys.stderr)

    # Escritura INCREMENTAL (ver iter_log_pages()): cada tanda se vuelca al
    # archivo de salida apenas llega, en vez de acumular todo en memoria y
    # escribir recien al final - asi, si una descarga larga se corta a
    # mitad de camino (ver _request_page_with_retries()), lo ya traido
    # queda guardado en vez de perderse por completo.
    written = 0
    last_ts_written = None
    try:
        with output_path.open("w", encoding="utf-8") as f:
            for batch in iter_log_pages(
                token, project_id, service_id, start, end,
                log_type=args.type, page_line_limit=args.line_limit,
            ):
                for e in batch:
                    text = e.get("log", "")
                    if grep_pattern and not grep_pattern.search(text):
                        continue
                    f.write(f"{e.get('ts', '')} {text}\n")
                    written += 1
                last_ts_written = batch[-1].get("ts")
                f.flush()
    except requests.exceptions.RequestException as exc:
        print(
            f"\nSe corto la descarga por un error de red ({exc.__class__.__name__}) despues de agotar "
            f"los reintentos. Lo ya traido SI quedo guardado: {written} lineas en {output_path.resolve()} "
            f"(hasta aproximadamente {last_ts_written or 'el inicio de la ventana'}). Volve a correr el "
            f"script - Northflank suele responder bien al segundo intento ante una caida transitoria.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"\nListo: {written} lineas guardadas en {output_path.resolve()} "
        f"(ventana: {args.hours:g}hs, tipo={args.type}).", file=sys.stderr,
    )


if __name__ == "__main__":
    main()
