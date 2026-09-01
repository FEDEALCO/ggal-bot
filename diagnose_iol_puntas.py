#!/usr/bin/env python3
"""
diagnose_iol_puntas.py
=======================
Diagnostico puntual (2026-09-01): el bot en Northflank esta reportando
0/52 cotizaciones "validas" (con punta vigente) en AMBOS vencimientos de
opciones de GGAL de forma sostenida durante horario de rueda, pero la
propia pagina de IOL muestra puntas reales (compra/venta distintas de
cero) para varios strikes en el mismo momento (ver captura del usuario).

Esto sugiere que `BrokerRestSource._parse_quote_record()` (ver
ggal_bot/data/live_shadow_feed.py) puede estar leyendo mal el campo
`puntas` de la respuesta REAL durante horario de rueda - la unica
corrida de referencia usada para confirmar ese parsing (diagnose_iol_api.py)
se hizo FUERA de horario, con `puntas: []` en todo, asi que la forma real
de un registro CON punta vigente nunca se inspecciono.

Este script:
    1. Login (igual que diagnose_iol_api.py).
    2. Trae la cadena de opciones de GGAL (mismo endpoint que usa el bot).
    3. Cuenta cuantos registros tienen 'puntas' no vacio.
    4. Imprime CRUDO (JSON) hasta 3 registros CON puntas no vacio y hasta
       2 SIN puntas, para comparar campo a campo contra lo que asume
       BrokerRestSource._parse_quote_record() (precioCompra/precioVenta/
       cantidadCompra/cantidadVenta dentro de puntas[0]).

No modifica nada, no envia ninguna orden - son 2 GET de solo lectura
contra la cuenta real (mismos endpoints que ya usa el bot en Shadow Mode).

Uso:
    python diagnose_iol_puntas.py [SIMBOLO]
"""

from __future__ import annotations

import json
import sys

from ggal_bot.config import SETTINGS
from ggal_bot.data import http_utils


def main() -> int:
    cfg = SETTINGS.broker_rest
    if not cfg.username or not cfg.password:
        print("ERROR: faltan BROKER_REST_USERNAME/BROKER_REST_PASSWORD en .env.")
        return 1

    underlying = sys.argv[1] if len(sys.argv) > 1 else SETTINGS.instruments.underlying_symbol
    version = f"{cfg.api_version_segment}/" if cfg.api_version_segment else ""

    print(f"1) Login POST {cfg.base_url.rstrip('/')}/token ...")
    token_url = cfg.base_url.rstrip("/") + "/token"
    payload = {"username": cfg.username, "password": cfg.password, "grant_type": "password"}
    try:
        token_data = http_utils.http_request_json(
            "POST", token_url, timeout=cfg.request_timeout_seconds, data=payload,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"   FALLO el login: {exc!r}")
        return 1
    token = token_data.get("access_token") if isinstance(token_data, dict) else None
    if not token:
        print("   ADVERTENCIA: no se encontro 'access_token' en la respuesta de login.")
        return 1
    print("   OK: login exitoso.")
    headers = {"Authorization": f"Bearer {token}"}

    options_url = f"{cfg.base_url.rstrip('/')}/api/{version}{cfg.market}/Titulos/{underlying}/Opciones"
    print(f"\n2) Cadena de opciones GET {options_url} ...")
    try:
        options_data = http_utils.http_get_json(options_url, timeout=cfg.request_timeout_seconds, headers=headers)
    except Exception as exc:  # noqa: BLE001
        print(f"   FALLO ({exc!r}).")
        return 1

    if not isinstance(options_data, list):
        print(f"   ADVERTENCIA: se esperaba una lista, se recibio {type(options_data)}.")
        print("   " + json.dumps(options_data, indent=2, ensure_ascii=False)[:2000])
        return 1

    print(f"   {len(options_data)} registros recibidos.")

    def has_puntas(rec):
        p = rec.get("puntas") if isinstance(rec, dict) else None
        if isinstance(p, list) and p:
            level = p[0]
        elif isinstance(p, dict) and p:
            level = p
        else:
            return False
        if not isinstance(level, dict):
            return False
        compra = level.get("precioCompra")
        venta = level.get("precioVenta")
        return bool((compra and compra > 0) or (venta and venta > 0))

    with_puntas = [r for r in options_data if has_puntas(r)]
    without_puntas = [r for r in options_data if not has_puntas(r)]

    print(f"\n3) Resumen: {len(with_puntas)}/{len(options_data)} registros tienen precioCompra o "
          f"precioVenta > 0 dentro de 'puntas' (segun el MISMO criterio que asume "
          f"BrokerRestSource._parse_quote_record()).")

    if with_puntas:
        print(f"\n4) Hasta 3 registros CON punta vigente (CRUDOS, comparar campo a campo):")
        for rec in with_puntas[:3]:
            print("   " + json.dumps(rec, indent=2, ensure_ascii=False).replace("\n", "\n   "))
    else:
        print(
            "\n4) NINGUN registro tiene punta vigente segun este parseo, a pesar de que la "
            "pagina de IOL muestra puntas reales para varios strikes ahora mismo - esto "
            "confirma un problema de PARSING (nombre de campo distinto al esperado), no de "
            "mercado. Mirando los primeros 3 registros CRUDOS sin filtrar, para inspeccionar "
            "manualmente que forma tiene 'puntas' de verdad:"
        )
        for rec in options_data[:3]:
            print("   " + json.dumps(rec, indent=2, ensure_ascii=False).replace("\n", "\n   "))

    if without_puntas and with_puntas:
        print(f"\n5) 1 registro SIN punta vigente (para contraste):")
        print("   " + json.dumps(without_puntas[0], indent=2, ensure_ascii=False).replace("\n", "\n   "))

    print(
        "\nListo. Si en el punto 4 el campo 'puntas' SI trae 'precioCompra'/'precioVenta' con "
        "valores > 0 pero BrokerRestSource igual los descarta, el bug esta en como se ACCEDE "
        "a esos valores (indentacion/anidamiento) en _parse_quote_record(), no en los nombres "
        "de campo en si - pegar esta salida completa para ajustarlo."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
