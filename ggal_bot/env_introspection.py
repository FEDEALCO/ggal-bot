"""
env_introspection.py
======================
Introspeccion de las env vars GGAL_BOT_* efectivas de ESTE proceso.

MEJORA 2026-09-30 (correccion de arquitectura, a pedido explicito del
usuario - ver REPORT.md): esto lo llama SOLO el BOT (run_bot.py, via
StateWriter.write(env_flags=...)) para publicar sus propias flags en
state/bot_state.json en cada ciclo. El dashboard NUNCA debe llamar a esto
ni leer os.environ por su cuenta (ver dashboard/data/bot_config.py) -
no hay garantia de que el dashboard corra en el mismo proceso/contenedor
que el bot (la topologia de un solo contenedor con procesos hermanos,
documentada en Dockerfile/entrypoint.sh, es un diseño, no algo verificado
contra la config real de Northflank en cada momento). La UNICA fuente de
verdad de "que flags tiene el bot ahora" es lo que el bot mismo publica.
"""
from __future__ import annotations

import os
from typing import Dict

_SENSITIVE_NAME_SUBSTRINGS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")


def list_ggal_bot_env_vars() -> Dict[str, str]:
    """
    Devuelve {nombre: valor_efectivo} de TODAS las env vars con prefijo
    GGAL_BOT_ presentes en os.environ de ESTE proceso, ordenadas
    alfabeticamente.

    Un nombre que matchea _SENSITIVE_NAME_SUBSTRINGS se enmascara ("***")
    en vez de mostrarse - a fecha 2026-09-30 ninguna env var GGAL_BOT_* es
    una credencial (verificado por grep en todo el repo: las credenciales
    de IOL/pyRofex usan otro prefijo, fuera del alcance de este listado),
    pero esta es la postura correcta por defecto para un valor que termina
    expuesto en un panel de solo lectura.
    """
    out: Dict[str, str] = {}
    for name in sorted(os.environ):
        if not name.startswith("GGAL_BOT_"):
            continue
        if any(s in name.upper() for s in _SENSITIVE_NAME_SUBSTRINGS):
            out[name] = "***"
        else:
            out[name] = os.environ[name]
    return out
