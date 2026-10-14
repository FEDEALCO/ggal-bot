#!/usr/bin/env python3
"""
diagnose_iol_caucion.py
==========================
Script de diagnostico standalone (mismo patron que diagnose_iol_api.py,
MISMO criterio de "nunca fabricar un endpoint/schema sin confirmarlo
contra una cuenta real antes de usarlo en produccion"). Se loguea contra
la API REAL de IOL con tus credenciales (BROKER_REST_USERNAME/PASSWORD en
.env) y prueba un puñado de rutas CANDIDATAS (sin confirmar) para la tasa
de caución, dejando constancia de cual devuelve 200 y con que forma.

POR QUE ESTE SCRIPT EXISTE (contexto completo, Fase 2 del mandato de
research cuantitativo, 2026-10-13/14, a pedido explicito del usuario -
item 3: "agregá el registro de la tasa de caución de IOL en cada ciclo"):
a diferencia de la cotizacion del subyacente/cadena de opciones
(diagnose_iol_api.py, endpoints CONFIRMADOS contra una cuenta real,
ggal_bot/data/live_shadow_feed.py::BrokerRestSource), este proyecto NO
tiene ningun endpoint de caucion confirmado ni inferido hasta ahora -
`grep -rn caucion ggal_bot/` solo encuentra comentarios sobre la TASA
default de Black-Scholes, nunca un cliente REST real.

INVESTIGACION REAL hecha antes de escribir este script (no se adivina a
ciegas): se uso el tool get_caucion_rates del MCP invertironline (fuente
DISTINTA, de uso exclusivo de Claude en este seguimiento, nunca disponible
para el bot en produccion) para confirmar el FORMATO de los datos (days,
due_date, rate, min_amount) - eso confirma que el DATO existe y que forma
tiene, pero NO revela la URL REST subyacente que ese MCP usa internamente,
asi que no sirve para que run_bot.py la llame directo.
Por busqueda web se encontro un UNICO ejemplo real (gist de un tercero:
github.com/alejoasotelo, "Buscar cauciones en invertir online") que pega
contra `POST https://iol.invertironline.com/Mercado/GetCaucionPuntas`
(moneda=PESOS, plazo=<dias>, idTipoTransaccion=14) - pero ESE es el
backend AJAX interno del sitio web (requiere cookie de sesion del login
WEB, header X-Requested-With), un mecanismo de autenticacion DISTINTO al
OAuth Bearer (POST /token) que ya usa BrokerRestSource para todo lo demas
en este proyecto. Mezclar ambos mecanismos de auth en el mismo cliente
seria una complejidad nueva no pedida (y una superficie fragil: un
endpoint interno sin documentar puede cambiar sin aviso) - por eso este
script prueba PRIMERO candidatos sobre la MISMA API publica/documentada
(Bearer token) que ya usa el resto del proyecto, y solo al final deja
constancia del hallazgo del endpoint AJAX interno, para que el usuario
decida con informacion completa.

Uso:
    python diagnose_iol_caucion.py

Requiere BROKER_REST_USERNAME/BROKER_REST_PASSWORD completos en .env (ver
.env.example) - un usuario y contraseña validos de tu cuenta de IOL.
"""

from __future__ import annotations

import json

from ggal_bot.config import SETTINGS
from ggal_bot.data import http_utils

# Candidatos SIN CONFIRMAR sobre la API publica (api.invertironline.com,
# Bearer token) - construidos por analogia con el UNICO patron de ruta que
# si esta confirmado en este proyecto (GET /api/v2/{market}/Titulos/
# {simbolo}/Cotizacion, ver diagnose_iol_api.py) mas los nombres de
# producto que usa el propio sitio de IOL ("Cauciones Colocadoras",
# "Cauciones Tomadoras" - ver invertironline.com/productos/cauciones).
# Ninguno de estos esta confirmado - este script existe precisamente para
# probarlos y descartar los que no respondan 200 con una forma razonable.
_CANDIDATE_PATHS = [
    "/api/{version}Cotizaciones-Cauciones/Cauciones/Argentina/Operables",
    "/api/{version}Cotizaciones-Cauciones/Cauciones/Argentina/Todos",
    "/api/{version}Cauciones/Cotizacion",
    "/api/{version}Cauciones",
]


def main() -> int:
    cfg = SETTINGS.broker_rest
    if not cfg.username or not cfg.password:
        print(
            "ERROR: faltan BROKER_REST_USERNAME/BROKER_REST_PASSWORD en .env.\n"
            "Completalos (tu usuario y contraseña reales de IOL) y volver a correr este script."
        )
        return 1

    version = f"{cfg.api_version_segment}/" if cfg.api_version_segment else ""

    print(f"1) Login POST {cfg.base_url.rstrip('/')}/token ...")
    token_url = cfg.base_url.rstrip("/") + "/token"
    payload = {"username": cfg.username, "password": cfg.password, "grant_type": "password"}
    try:
        token_data = http_utils.http_request_json(
            "POST", token_url, timeout=cfg.request_timeout_seconds, data=payload,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostico: se quiere ver el error tal cual
        print(f"   FALLO el login: {exc!r}")
        return 1

    token = token_data.get("access_token") if isinstance(token_data, dict) else None
    if not token:
        print("   FALLO: no se encontro 'access_token' en la respuesta del login.")
        return 1
    print("   OK: login exitoso.")
    headers = {"Authorization": f"Bearer {token}"}

    print(
        "\n2) Probando rutas CANDIDATAS (NINGUNA confirmada todavia) sobre la API publica "
        "(Bearer token, misma auth que el resto del proyecto):"
    )
    any_ok = False
    for template in _CANDIDATE_PATHS:
        path = template.format(version=version)
        url = cfg.base_url.rstrip("/") + path
        print(f"\n   GET {url} ...")
        try:
            data = http_utils.http_get_json(url, timeout=cfg.request_timeout_seconds, headers=headers)
            any_ok = True
            print("   OK - respuesta CRUDA:")
            dumped = json.dumps(data, indent=2, ensure_ascii=False)
            print("   " + dumped[:2000].replace("\n", "\n   ") + ("..." if len(dumped) > 2000 else ""))
        except Exception as exc:  # noqa: BLE001
            print(f"   FALLO ({exc!r}).")

    if not any_ok:
        print(
            "\n   Ninguna ruta candidata respondio - no hay evidencia de un endpoint de caucion "
            "en la API publica Bearer-token con estos nombres. Revisar la documentacion oficial "
            "de IOL (https://api.invertironline.com/Help, requiere estar logueado en el navegador "
            "para verla) o el soporte de IOL directamente."
        )

    print(
        "\n3) HALLAZGO DE REFERENCIA (NO integrado al bot - mecanismo de auth DISTINTO, ver "
        "docstring de este modulo): un tercero documento el backend AJAX interno del SITIO WEB "
        "(no la API publica) en https://gist.github.com/alejoasotelo/beb1d357278d6b591b3bd0ba4e465a75 :\n"
        "   POST https://iol.invertironline.com/Mercado/GetCaucionPuntas\n"
        "   body (form-urlencoded): moneda=PESOS, plazo=<dias 1-30>, idTipoTransaccion=14\n"
        "   headers: X-Requested-With: XMLHttpRequest, credentials: include (cookie de sesion WEB)\n"
        "   respuesta: {{'plazo': ..., 'listaPuntas': [{{'precioCompra': <tasa>, 'cantidadCompra': <monto>}}, ...]}}\n"
        "   Esto NO usa el Bearer token de arriba - requeriria manejar una sesion de cookies del "
        "sitio web por separado, una integracion nueva y no documentada oficialmente (puede romper "
        "sin aviso). Se deja constancia por transparencia, no se recomienda construir sobre esto "
        "sin que el usuario lo decida explicitamente."
    )
    return 0 if any_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
