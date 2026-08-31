#!/usr/bin/env python3
"""
diagnose_iol_api.py
=====================
Script de diagnostico standalone: se loguea contra la API real de IOL
(InvertirOnline) con tus credenciales (BROKER_REST_USERNAME/PASSWORD en
.env) y vuelca las respuestas CRUDAS (JSON) de:

    1. Login (POST /token)
    2. Cotizacion del subyacente GGAL
    3. Cadena de opciones de GGAL

El objetivo es el mismo que diagnose_instruments.py pero para
BrokerRestSource (ver ggal_bot/data/live_shadow_feed.py): el mecanismo de
login (POST /token, form-urlencoded, grant_type=password, respuesta con
'access_token') esta CONFIRMADO contra la documentacion oficial de IOL
(https://api.invertironline.com/Help/Autenticacion), pero los endpoints y
nombres de campo de cotizacion/opciones que asume BrokerRestSource
(`_parse_quote_record()`/`bootstrap()`) fueron INFERIDOS de clientes de
codigo abierto de terceros, no ejercitados por este proyecto contra una
cuenta real - correr este script es la forma de confirmarlos (o detectar
que hay que ajustarlos) ANTES de usar "broker_rest" como fuente Shadow.

Si algun campo impreso aca no coincide con lo que asume
BrokerRestSource._parse_quote_record()/bootstrap() en
ggal_bot/data/live_shadow_feed.py, ajustar esos metodos - son los UNICOS
lugares del proyecto con parsing especifico de IOL.

Uso:
    python diagnose_iol_api.py [SIMBOLO]

    SIMBOLO es opcional (default: GGAL_BOT... instruments.underlying_symbol,
    "GGAL" salvo que se haya cambiado en .env).

Requiere BROKER_REST_USERNAME/BROKER_REST_PASSWORD completos en .env (ver
.env.example) - un usuario y contraseña validos de tu cuenta de IOL.
"""

from __future__ import annotations

import json
import sys

from ggal_bot.config import SETTINGS
from ggal_bot.data import http_utils


def main() -> int:
    cfg = SETTINGS.broker_rest
    if not cfg.username or not cfg.password:
        print(
            "ERROR: faltan BROKER_REST_USERNAME/BROKER_REST_PASSWORD en .env.\n"
            "Completalos (tu usuario y contraseña reales de IOL) y volver a correr este script."
        )
        return 1

    underlying = sys.argv[1] if len(sys.argv) > 1 else SETTINGS.instruments.underlying_symbol
    version = f"{cfg.api_version_segment}/" if cfg.api_version_segment else ""

    # --- 1. Login ------------------------------------------------------
    print(f"1) Login POST {cfg.base_url.rstrip('/')}/token ...")
    token_url = cfg.base_url.rstrip("/") + "/token"
    payload = {"username": cfg.username, "password": cfg.password, "grant_type": "password"}
    try:
        token_data = http_utils.http_request_json(
            "POST", token_url, timeout=cfg.request_timeout_seconds, data=payload,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostico: se quiere ver el error tal cual, no degradarlo
        print(f"   FALLO el login: {exc!r}")
        print("   Revisar usuario/contraseña, o si tu cuenta requiere un paso adicional (ej. 2FA) no cubierto aca.")
        return 1

    print("   Respuesta CRUDA del login (comparar contra 'access_token' que asume BrokerRestSource._login()):")
    print("   " + json.dumps(token_data, indent=2, ensure_ascii=False).replace("\n", "\n   "))

    token = token_data.get("access_token") if isinstance(token_data, dict) else None
    if not token:
        print(
            "\n   ADVERTENCIA: no se encontro la clave 'access_token' en la respuesta de arriba - "
            "ajustar BrokerRestSource._login() en ggal_bot/data/live_shadow_feed.py con el nombre real."
        )
        return 1
    print("   OK: 'access_token' encontrado y usable.")

    headers = {"Authorization": f"Bearer {token}"}

    # --- 2. Cotizacion del subyacente -----------------------------------
    quote_url = f"{cfg.base_url.rstrip('/')}/api/{version}{cfg.market}/Titulos/{underlying}/Cotizacion"
    print(f"\n2) Cotizacion GET {quote_url} ...")
    try:
        quote_data = http_utils.http_get_json(quote_url, timeout=cfg.request_timeout_seconds, headers=headers)
        print("   Respuesta CRUDA (comparar contra BrokerRestSource._parse_quote_record()):")
        print("   " + json.dumps(quote_data, indent=2, ensure_ascii=False).replace("\n", "\n   "))
    except Exception as exc:  # noqa: BLE001
        print(f"   FALLO ({exc!r}).")
        if cfg.api_version_segment:
            print(
                f"   Si es un 404, probar BROKER_REST_API_VERSION_SEGMENT= (vacio) en .env - algunos clientes "
                f"documentan '/api/{cfg.market}/...' sin el segmento de version."
            )

    # --- 3. Cadena de opciones -------------------------------------------
    options_url = f"{cfg.base_url.rstrip('/')}/api/{version}{cfg.market}/Titulos/{underlying}/Opciones"
    print(f"\n3) Cadena de opciones GET {options_url} ...")
    try:
        options_data = http_utils.http_get_json(options_url, timeout=cfg.request_timeout_seconds, headers=headers)
        count = len(options_data) if isinstance(options_data, list) else "?"
        print(f"   {count} registros recibidos. Primeros hasta 3, CRUDOS (comparar contra BrokerRestSource.bootstrap()):")
        sample = options_data[:3] if isinstance(options_data, list) else options_data
        print("   " + json.dumps(sample, indent=2, ensure_ascii=False).replace("\n", "\n   "))
    except Exception as exc:  # noqa: BLE001
        print(f"   FALLO ({exc!r}).")
        if cfg.api_version_segment:
            print(
                f"   Si es un 404, probar BROKER_REST_API_VERSION_SEGMENT= (vacio) en .env - algunos clientes "
                f"documentan '/api/{cfg.market}/...' sin el segmento de version."
            )

    print(
        "\nListo. Si algun nombre de campo de arriba no coincide con lo que asumen "
        "BrokerRestSource._parse_quote_record()/bootstrap() (ggal_bot/data/live_shadow_feed.py), "
        "ajustar esos metodos antes de agregar 'broker_rest' a GGAL_BOT_SHADOW_SOURCE_PRIORITY."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
