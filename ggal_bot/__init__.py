"""
ggal_bot
========
Bot de trading cuantitativo de opciones sobre GGAL (BYMA), basado en la
filosofia de volatilidad, delta-neutralidad y gestion de griegas de
Ricardo Saenz de Heredia.

Estructura del paquete (mismo esquema modular usado en Quantbot):
    config.py       -> parametros de configuracion (limites, simbolos, tasas)
    paths.py        -> resolucion de rutas (logs, estado, datos)
    state_writer.py -> persistencia periodica del estado del bot (JSON)
    data/           -> conexion a mercado (PyRofex) y armado de la cadena de opciones
    models/         -> Black-Scholes, solver de IV, vol historica, superficie de vol
    portfolio/      -> posiciones y agregacion de griegas
    execution/      -> market making (mid-price), delta hedging, gateway de ordenes
    risk/           -> limites de riesgo y filtros de liquidez
    strategy/       -> deteccion de descalibres de smile y armado de señales
    validation/     -> tests de sanity (paridad put-call, convergencia del solver, etc.)
"""

__version__ = "0.1.0"
