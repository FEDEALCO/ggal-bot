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


def fetch_logs(
    token: str, project_id: str, service_id: str,
    start: datetime, end: datetime, log_type: str = "runtime",
    page_line_limit: int = 1000,  # tope maximo real de la API (ver main(): 2000 daba 400)
) -> list[dict]:
    """
    Trae TODOS los logs entre start y end, paginando hacia adelante (la API
    de Northflank no documenta un cursor - se pagina avanzando el
    startTime al timestamp del ultimo log recibido en cada tanda, hasta
    que una tanda vuelve vacia o mas corta que el limite de pagina).
    """
    url = f"{API_BASE}/projects/{project_id}/services/{service_id}/logs"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    all_entries: list[dict] = []
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
        resp = requests.get(url, headers=headers, params=params, timeout=30)
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

        all_entries.extend(batch)

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

    return all_entries


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

    print(f"Trayendo logs tipo='{args.type}' desde {_iso(start)} hasta {_iso(end)}...", file=sys.stderr)
    entries = fetch_logs(token, project_id, service_id, start, end, log_type=args.type, page_line_limit=args.line_limit)

    if args.grep:
        import re
        pattern = re.compile(args.grep)
        entries = [e for e in entries if pattern.search(e.get("log", ""))]

    output_path = Path(args.output) if args.output else Path(
        f"northflank_logs_{args.hours:g}h_{end.strftime('%Y%m%dT%H%M%SZ')}.log"
    )
    with output_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(f"{e.get('ts', '')} {e.get('log', '')}\n")

    print(
        f"\nListo: {len(entries)} lineas guardadas en {output_path.resolve()} "
        f"(ventana: {args.hours:g}hs, tipo={args.type}).", file=sys.stderr,
    )


if __name__ == "__main__":
    main()
