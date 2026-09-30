"""
dashboard/data/bot_config.py
==============================
Introspeccion pura y testeada de: (1) las env vars GGAL_BOT_* efectivas
del proceso (para el panel "Salud del bot"/"flags activas" - el dashboard
corre como proceso HERMANO del bot en el mismo contenedor, ver
entrypoint.sh, asi que comparte el mismo os.environ, sin gap), y (2) el
snapshot en vivo state/bot_state.json (ver ggal_bot/state_writer.py).

SEGURIDAD: aunque a fecha 2026-09-30 ninguna env var con prefijo
GGAL_BOT_* es una credencial (verificado via grep sobre todo el repo -
las credenciales de IOL usan otro prefijo, fuera del alcance de este
listado), este modulo enmascara por las dudas cualquier variable cuyo
NOMBRE contenga una palabra sensible, para que un futuro env var mal
nombrado nunca se muestre en texto plano en el dashboard.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from ggal_bot import paths

_SENSITIVE_NAME_SUBSTRINGS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")


def list_ggal_bot_env_vars() -> Dict[str, str]:
    """
    Devuelve {nombre: valor_efectivo} de TODAS las env vars con prefijo
    GGAL_BOT_ presentes en os.environ de ESTE proceso, ordenadas
    alfabeticamente. El dashboard comparte el mismo entorno del bot
    (procesos hermanos en el mismo contenedor - ver entrypoint.sh), asi que
    esto refleja exactamente la config con la que el bot esta corriendo,
    no un archivo de config separado que podria estar desactualizado.

    Un nombre que matchea _SENSITIVE_NAME_SUBSTRINGS se enmascara
    ("***") en vez de mostrarse - nunca hubo necesidad de esto hasta
    ahora (ninguna GGAL_BOT_* es una credencial), pero es la postura
    correcta por defecto para un panel de solo lectura.
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


def load_bot_state(json_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """
    Lee state/bot_state.json (ver ggal_bot/state_writer.py::StateWriter.write) -
    snapshot de UN SOLO punto en el tiempo (no historico): griegas de
    cartera, señales activas, risk_breaches, option_chain_snapshot.

    Devuelve None si el archivo no existe, esta vacio o no es JSON valido -
    NUNCA fabrica un snapshot ni devuelve un dict con valores por defecto.
    El llamador debe mostrar "SIN DATOS" en ese caso.
    """
    path = Path(json_path) if json_path is not None else paths.STATE_FILE
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data
