#!/usr/bin/env python3
"""
diagnose_instruments.py
=========================
Script de diagnostico standalone: se conecta al ambiente configurado (ver
.env) y vuelca el listado de instrumentos que devuelve pyRofex, para poder
calibrar InstrumentsConfig contra lo que realmente entrega tu ALYC.

v2: la primera version buscaba "GGAL"/"GFG" como texto DENTRO del simbolo,
lo cual solo encontro la accion y sus variantes de plazo (GGAL/24hs,
GGALD, etc.) pero ningun contrato de opcion - la corrida real del usuario
mostro 878 instrumentos con 0 opciones matcheando por nombre. Como la
respuesta de pyRofex.get_detailed_instruments() incluye campos semanticos
(underlying, cficode, strike, maturityDate) para TODOS los instrumentos, no
solo para las opciones, este script ahora busca por esos campos en vez de
adivinar el patron del simbolo:

    1. underlying == 'GGAL' (case-insensitive)              -> es un
       derivado de GGAL, sea futuro u opcion.
    2. cficode empieza con 'O' (ISO 10962: Option) o strike
       viene con un valor numerico > 0                       -> ademas
       es especificamente una OPCION (no un futuro/CI/24hs).

Esto encuentra las opciones de GGAL sin importar como este armado el
simbolo (a diferencia de la v1, que dependia de que el simbolo contuviera
"GGAL" o "GFG" como texto).

Uso:
    python diagnose_instruments.py

Genera:
    - Salida por consola con el resumen y el detalle de lo encontrado.
    - instruments_sample.json en la carpeta del proyecto con el volcado
      completo para inspeccion manual si hace falta.
"""

from __future__ import annotations

import json
import sys

from ggal_bot.config import SETTINGS
from ggal_bot.data.market_data_feed import MarketDataFeed, _bare_symbol
from ggal_bot.execution.order_gateway import initialize_environment


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    print(f"Inicializando ambiente PyRofex ({SETTINGS.broker.environment})...")
    if not initialize_environment():
        print("ERROR: no se pudo inicializar el ambiente. Revisar .env (usuario/password/cuenta).")
        return 1

    feed = MarketDataFeed(on_book_update=lambda *_: None)
    instruments = feed._fetch_instruments()
    if not instruments:
        print("ERROR: pyRofex no devolvio ningun instrumento (ver logs de WARNING/ERROR arriba).")
        return 1

    print(f"\nTotal de instrumentos recibidos: {len(instruments)}")

    underlying_symbol = SETTINGS.instruments.underlying_symbol.upper()  # "GGAL"

    # --- Paso 1: TODOS los derivados/instrumentos cuyo 'underlying' sea GGAL,
    # sin importar el texto del simbolo (esto es lo que la v1 del script no
    # veia: si el simbolo de una opcion no contiene "GGAL" como texto, buscar
    # por substring del simbolo nunca la iba a encontrar). ---
    ggal_related = []
    for inst in instruments:
        underlying = str(inst.get("underlying") or "").upper()
        if underlying == underlying_symbol:
            ggal_related.append(inst)

    print(f"\n--- Instrumentos con underlying == '{underlying_symbol}': {len(ggal_related)} ---")

    # --- Paso 2: de esos, cuales son especificamente OPCIONES (cficode tipo
    # Option segun ISO 10962, o con un strike numerico > 0) vs. futuros/CI. ---
    options_found = []
    non_option_derivatives = []
    for inst in ggal_related:
        cficode = str(inst.get("cficode") or "").upper()
        strike = _to_float(inst.get("strike"))
        looks_like_option = cficode.startswith("O") or strike > 0
        (options_found if looks_like_option else non_option_derivatives).append(inst)

    print(f"    de los cuales parecen OPCIONES (cficode='O...' o strike>0): {len(options_found)}")
    print(f"    de los cuales parecen futuros/CI/otros (sin strike, cficode no-opcion): {len(non_option_derivatives)}")

    if options_found:
        print("\n--- Detalle de las opciones de GGAL encontradas ---")
        for inst in options_found[:80]:
            symbol = feed._extract_symbol(inst) or "<sin simbolo>"
            print(
                f"  simbolo={symbol!r}  bare={_bare_symbol(symbol)!r}  "
                f"cficode={inst.get('cficode')!r}  strike={inst.get('strike')!r}  "
                f"maturityDate={inst.get('maturityDate')!r}  securityType={inst.get('securityType')!r}"
            )
    else:
        print(
            "\nNo se encontro NINGUNA opcion de GGAL (ni por cficode ni por strike) entre "
            f"los {len(ggal_related)} instrumentos con underlying == '{underlying_symbol}'.\n"
            "Esto sugiere que este ambiente/cuenta (REMARKET) puede no tener aprovisionada la\n"
            "cadena de opciones de GGAL - es comun que el ambiente de paper trading de un ALYC\n"
            "solo incluya futuros/indices/CI y no replique el book completo de opciones de BYMA.\n"
            "Conviene confirmar con tu ALYC si las opciones de GGAL estan disponibles via esta\n"
            "API en REMARKET, y si no, si hay forma de consultarlas solo para datos (sin operar)\n"
            "contra el ambiente LIVE."
        )
        if non_option_derivatives:
            print(f"\n(Si sirve de referencia, esto es lo que SI aparece bajo underlying='{underlying_symbol}':)")
            for inst in non_option_derivatives[:20]:
                symbol = feed._extract_symbol(inst) or "<sin simbolo>"
                print(f"  {symbol!r}  (securityType={inst.get('securityType')!r}, cficode={inst.get('cficode')!r})")

    # --- Referencia adicional: valores distintos de 'underlying' presentes
    # en todo el listado, para chequear que "GGAL" sea efectivamente como
    # este ALYC nombra al subyacente (por si usa otra convencion, ej. el ISIN). ---
    distinct_underlyings = sorted({
        str(inst.get("underlying")) for inst in instruments if inst.get("underlying")
    })
    print(f"\n--- Valores distintos de 'underlying' en todo el listado: {len(distinct_underlyings)} ---")
    sample_preview = [u for u in distinct_underlyings if "GGAL" in u.upper()] or distinct_underlyings[:20]
    for u in sample_preview[:20]:
        print(f"  {u!r}")

    sample_path = "instruments_sample.json"
    with open(sample_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "total_instruments": len(instruments),
                "ggal_related": ggal_related,
                "options_found": options_found,
                "distinct_underlyings": distinct_underlyings,
                "first_500_all": instruments[:500],
            },
            f, indent=2, ensure_ascii=False, default=str,
        )
    print(f"\nVolcado completo guardado en: {sample_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
